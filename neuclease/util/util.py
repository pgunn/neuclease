import os
import re
import io
import sys
import copy
import time
import math
import json
import vigra
import logging
import inspect
import contextlib
from functools import partial, lru_cache
from multiprocessing.pool import Pool, ThreadPool
from datetime import datetime, timedelta
from itertools import product, starmap
from collections.abc import Mapping, Iterable, Iterator, Sequence

import ujson
import requests
from tqdm import tqdm

# Disable the monitor thread entirely.
# It is more trouble than it's worth, especially when using tqdm_proxy, below.
tqdm.monitor_interval = 0

import numpy as np
import pandas as pd
from numba import jit

from .downsample_with_numba import downsample_binary_3d_suppress_zero
from .box import extract_subvol, box_intersection, round_coord
from .view_as_blocks import view_as_blocks

logger = logging.getLogger(__name__)

@contextlib.contextmanager
def Timer(msg=None, logger=None, level=logging.INFO):
    """
    Simple context manager that acts as a wall-clock timer.
    
    Args:
        msg:
            Optional message to be logged at the start
            and stop of the timed period.
        logger:
            Which logger to write the message to.

    Example:
        >>> with Timer("Doing stuff") as timer:
        ...     # do stuff here
        >>>
        >>> print(timer.seconds)
        >>> print(timer.timedelta)
    """
    if msg:
        logger = logger or logging.getLogger(__name__)
        logger.log(level, msg + '...')
    result = _TimerResult()
    yield result
    result.stop = time.time()
    if msg:
        logger.log(level, msg + f' took {result.timedelta}')


class _TimerResult(object):
    """
    Helper class, yielded by the Timer context manager.
    """
    def __init__(self):
        self.start = time.time()
        self.stop = None

    @property
    def seconds(self):
        if self.stop is None:
            return time.time() - self.start
        else:
            return self.stop - self.start

    @property
    def timedelta(self):
        return timedelta(seconds=self.seconds)


def uuids_match(uuid1, uuid2):
    """
    Return True if the two uuids are the equivalent.
    
    >>> assert uuids_match('abcd', 'abcdef') == True
    >>> assert uuids_match('abc9', 'abcdef') == False
    """
    assert uuid1 and uuid2, "Empty UUID"
    n = min(len(uuid1), len(uuid2))
    return (uuid1[:n] == uuid2[:n])


def fetch_file(url, output=None, chunksize=2**10, *, session=None):
    """
    Fetch a file from the given endpoint,
    and save it to bytes, a file object, or a file path.

    Args:
        url:
            Complete url to fetch from.
        
        output:
            If None, file is returned in-memory, as bytes.
            If str, it is interpreted as a path to which the file will be written.
            Otherwise, must be a file object to write the bytes to (e.g. a BytesIO object).
        
        chunksize:
            Data will be streamed in chunks, with the given chunk size.

    Returns:
        None, unless no output file object/path is provided,
        in which case the fetched bytes are returned.
    """
    session = session or requests.Session()
    with session.get(url, stream=True) as r:
        r.raise_for_status()

        if output is None:
            return r.content

        if isinstance(output, str):
            # Create a file on disk and write to it.
            with open(output, 'wb') as f:
                for chunk in r.iter_content(chunksize):
                    f.write(chunk)
        else:
            # output is a file object
            for chunk in r.iter_content(chunksize):
                output.write(chunk)


def post_file(url, f, *, session=None):
    """
    Args:
        url:
            Complete url to which the file will be posted.
        f:
            The file to post.
            Either a path to a file, a (binary) file object,
            or a bytes object.
    """
    session = session or requests.Session()
    if isinstance(f, str):
        fname = f
        with open(fname, 'rb') as f:
            r = session.post(url, data=f)
    else:
        # Either bytes or a file object
        r = session.post(url, data=f)

    r.raise_for_status()


def load_df(npy_path):
    return pd.DataFrame(np.load(npy_path, allow_pickle=True))


@contextlib.contextmanager
def switch_cwd(d, create=False):
    """
    Context manager.
    chdir into the given directory (creating it first if desired),
    and exit back to the original CWD after the context manager exits.
    """
    if create:
        os.makedirs(d, exist_ok=True)
    old_dir = os.getcwd()
    os.chdir(d)
    yield
    os.chdir(old_dir)


class ndrange:
    """
    Generator.

    Like np.ndindex, but accepts start/stop/step instead of
    assuming that start is always (0,0,0) and step is (1,1,1).
    
    Example:
    
    >>> for index in ndrange((1,2,3), (10,20,30), step=(5,10,15)):
    ...     print(index)
    (1, 2, 3)
    (1, 2, 18)
    (1, 12, 3)
    (1, 12, 18)
    (6, 2, 3)
    (6, 2, 18)
    (6, 12, 3)
    (6, 12, 18)
    
    See also: ``ndindex_array()``
    """

    def __init__(self, start, stop=None, step=None):
        if stop is None:
            stop = start
            start = (0,)*len(stop)
    
        if step is None:
            step = (1,)*len(stop)
    
        assert len(start) == len(stop) == len(step), \
            f"tuple lengths don't match: ndrange({start}, {stop}, {step})"

        self.start = start
        self.stop = stop
        self.step = step
    
    def __iter__(self):
        return product(*starmap(range, zip(self.start, self.stop, self.step)))

    def __len__(self):
        span = (np.array(self.stop) - self.start)
        step = np.array(self.step)
        return np.prod( (span + step-1) // step )


def ndrange_array(start, stop=None, step=None):
    """
    Like np.ndindex, but accepts start/stop/step instead of
    assuming that start is always (0,0,0) and step is (1,1,1),
    and returns an array instead of an iterator.
    """
    if stop is None:
        stop = start
        start = (0,)*len(stop)

    start, stop = box = np.array((start, stop))
    aligned_box = box - start
    if step is None:
        # Step is implicitly 1
        shape = aligned_box[1]
        return start + ndindex_array(*shape)
    else:
        shape = round_coord(aligned_box[1], step, 'up') // step
        return start + step * ndindex_array(*shape)


def ndindex_array(*shape, dtype=np.int32):
    """
    Like np.ndindex, but returns an array.
    
    numpy has no convenience function for this, and won't any time soon.
    https://github.com/numpy/numpy/issues/1234#issuecomment-545990743
    
    Example:
    
        >>> ndindex_array(3,4)
        array([[0, 0],
               [0, 1],
               [0, 2],
               [0, 3],
               [1, 0],
               [1, 1],
               [1, 2],
               [1, 3],
               [2, 0],
               [2, 1],
               [2, 2],
               [2, 3]])
    """
    return np.indices(shape, dtype=dtype).reshape(len(shape), -1).transpose()


class NumpyConvertingEncoder(json.JSONEncoder):
    """
    Encoder that converts numpy arrays and scalars
    into their pure-python counterparts.
    
    (No attempt is made to preserve bit-width information.)
    
    Usage:
    
        >>> d = {"a": np.arange(3, dtype=np.uint32)}
        >>> json.dumps(d, cls=NumpyConvertingEncoder)
        '{"a": [0, 1, 2]}'
    """
    def default(self, o):
        if isinstance(o, (np.ndarray, np.number)):
            return o.tolist()
        return super().default(o)


def dump_json(obj, f=None, indent=2, convert_nans=False, unsplit_int_lists=False):
    """
    Pretty-print the given object to json, either to a file or to a returned string.

    obj:
        Object to serialize to json.
        Permitted to contain numpy arrays.

    f:
        A file handle to write to, or a file path to create,
        or None, in which case the json is returned as a string.

    convert_nans:
        If True, replace NaN values with a string "NaN".
        Otherwise, the default python behavior is to write the word NaN
        (without quotes) into the json file, which is not compliant with
        the json standard.

    unsplit_int_lists:
         When pretty-printing, json splits lists of integers (e.g. [123, 456, 789])
         across several lines.  For short lists, this is undesirable.
         This option will "unsplit" those lists, putting them back on a single line.
         This is implemented as a post-processing step using text matching.
         Might not be fast for huge files.

    Returns:
        Nothing if f was provided, otherwise returns a string.
    """
    if convert_nans:
        obj = _convert_nans(obj)

    kwargs = dict(indent=indent,
                  allow_nan=not convert_nans,
                  cls=NumpyConvertingEncoder)

    if unsplit_int_lists:
        json_text = json.dumps(obj, **kwargs)
        json_text = unsplit_json_int_lists(json_text)

        if isinstance(f, str):
            with open(f, 'w') as f:
                f.write(json_text)
        elif f:
            f.write(json_text)
        else:
            return json_text
    else:
        if isinstance(f, str):
            with open(f, 'w') as f:
                json.dump(obj, f, **kwargs)
        elif f:
            json.dump(obj, f, **kwargs)
        else:
            return json.dumps(obj, **kwargs)


def convert_nans(o, _c=None):
    """
    Traverse the given collection-of-collections and
    replace all NaN values with the string "NaN".
    Also converts numpy arrays into lists.
    Intended for preprocessing objects before JSON serialization.
    """
    _c = _c or {}

    if isinstance(o, float) and math.isnan(o):
        return "NaN"
    elif isinstance(o, np.number):
        if np.isnan(o):
            return "NaN"
        return o.tolist()
    elif isinstance(o, (str, bytes)) or not isinstance(o, (Sequence, Mapping)):
        return o

    # Even though this function is meant mostly for JSON,
    # so we aren't likely to run into self-referencing
    # or cyclical object graphs, we handle that case by keeping
    # track of the objects we've already processed.
    if id(o) in _c:
        return _c[id(o)]

    if isinstance(o, np.ndarray):
        ret = []
        _c[id(o)] = ret
        ret.extend([convert_nans(x, _c) for x in o.tolist()])
    elif isinstance(o, Sequence):
        ret = []
        _c[id(o)] = ret
        ret.extend([convert_nans(x, _c) for x in o])
    elif isinstance(o, Mapping):
        ret = {}
        _c[id(o)] = ret
        ret.update({k: convert_nans(v, _c) for k,v in o.items()})
    else:
        raise RuntimeError(f"Can't handle {type(o)} object: {o}")

    return ret


# used in json_dump(), above
_convert_nans = convert_nans


def unsplit_json_int_lists(json_text):
    """
    When pretty-printing json data, it will split all lists across several lines.
    For small lists of integers (such as [x,y,z] points), that may not be desirable.
    This function "unsplits" all lists of integers and puts them back on a single line.

    Example:
        >>> s = '''\\
        ... {
        ...   "body": 123,
        ...   "supervoxel": 456,
        ...   "coord": [
        ...     123,
        ...     456,
        ...     789
        ...   ],
        ... }
        ... '''

        >>> u = unsplit_json_int_lists(s)
        >>> print(u)
        {
        "body": 123,
        "supervoxel": 456,
        "coord": [123,456, 781],
        }

    """
    json_text = re.sub(r'\[\s+(\d+),', r'[\1,', json_text)
    json_text = re.sub(r'\n\s*(\d+),', r' \1,', json_text)
    json_text = re.sub(r'\n\s*(\d+)\s*\]', r' \1]', json_text)
    return json_text


def write_json_list(objects, f):
    """
    Like json.dump(), but writes each item to its own line (no indentation).
    """
    assert isinstance(objects, list)

    def _impl(f):
        f.write('[\n')
        for s in objects[:-1]:
            ujson.dump(s, f)
            f.write(',\n')
        ujson.dump(objects[-1], f)
        f.write('\n]')

    if isinstance(f, str):
        with open(f, 'w') as fp:
            _impl(fp)
    else:
        _impl(f)


def gen_json_objects(f, batch_size=None, parse=True):
    """
    Generator.
    
    Given a file containing a JSON list-of-objects,
    parse the objects one-by-one and iterate over them.
    
    Args:
        f:
            A file containing a JSON document which must be a list-of-objects.
            Must be an actual on-disk file (or a path to one),
            becuase it will be memory-mapped and therefore must have a fileno(). 

        batch_size:
            If provided, the objects will be yielded in groups
            (lists) of the specified size.

        parse:
            If True, each json object will be parsed and yielded as a dict.
            Otherwise, the raw text of the object is returned.
    """
    m = np.memmap(f, mode='r')
    it = map(bytes, _gen_json_objects(m))
    
    if parse:
        it = map(ujson.loads, it)
        
    if batch_size is None:
        yield from it
    else:
        yield from iter_batches(it, batch_size)


@jit(nopython=True, nogil=True)
def _gen_json_objects(text_array):
    """
    Generator.
    
    Parse a JSON list-of-objects one at a time,
    without reading in the entire file at once.
    
    Each object is yielded and then discarded.
    
    Warnings:
        - The input MUST be valid JSON, and specifically must be a list-of-objects.
          Any other input results in undefined behavior and/or errors.
        - Strings containing curly braces are not supported.
          (The document must not contain any curly braces except for the ones
          defining actual JSON objects.)
        
    Args:
        text_array:
            A np.array (dtype == np.uint8) which, when interpreted as text,
            contains a list-of-dicts JSON document.
    
    Yields:
        Every object in the document, one at a time.
    """
    nest_level = 0
    cur_start = 0
    cur_stop = 0
    for i, c in enumerate(text_array):
        if c == b'{'[0]:
            if nest_level == 0:
                cur_start = i
            nest_level += 1
        if c == b'}'[0]:
            nest_level -= 1
            if nest_level == 0:
                cur_stop = i+1
                yield text_array[cur_start:cur_stop]


def iter_batches(it, batch_size):
    """
    Iterator.
    
    Consume the given iterator/iterable in batches and
    yield each batch as a list of items.
    
    The last batch might be smaller than the others,
    if there aren't enough items to fill it.
    
    If the given iterator supports the __len__ method,
    the returned batch iterator will, too.
    """
    if hasattr(it, '__len__'):
        return _iter_batches_with_len(it, batch_size)
    else:
        return _iter_batches(it, batch_size)


class _iter_batches:
    def __init__(self, it, batch_size):
        self.base_iterator = it
        self.batch_size = batch_size
                

    def __iter__(self):
        return self._iter_batches(self.base_iterator, self.batch_size)
    

    def _iter_batches(self, it, batch_size):
        if isinstance(it, (pd.DataFrame, pd.Series)):
            for batch_start in range(0, len(it), batch_size):
                yield it.iloc[batch_start:batch_start+batch_size]
            return
        elif isinstance(it, (list, np.ndarray)):
            for batch_start in range(0, len(it), batch_size):
                yield it[batch_start:batch_start+batch_size]
            return
        else:
            if not isinstance(it, Iterator):
                assert isinstance(it, Iterable)
                it = iter(it)
    
            while True:
                batch = []
                try:
                    for _ in range(batch_size):
                        batch.append(next(it))
                except StopIteration:
                    return
                finally:
                    if batch:
                        yield batch


class _iter_batches_with_len(_iter_batches):
    def __len__(self):
        return int(np.ceil(len(self.base_iterator) / self.batch_size))


def compute_parallel(func, iterable, chunksize=1, threads=None, processes=None, ordered=True,
                     leave_progress=False, total=None, initial=0, starmap=False, show_progress=None,
                     **pool_kwargs):
    """
    Use the given function to process the given iterable in a ThreadPool or process Pool,
    showing progress using tqdm.
    
    Args:
        func:
            The function to process each item with.

        iterable:
            The items to process.

        chunksize:
            Send items to the pool in chunks of this size.

        threads:
            If given, use a ThreadPool with this many threads.

        processes
            If given, use a multiprocessing Pool with this many processes.
            Note: When using a process pool, your function and iterable items must be pickleable.

        ordered:
            If True, process the items in order, and return results
            in the same order as provded in the input.
            If False, process the items as quickly as possible,
            meaning that some results will be presented out-of-order,
            depending on how long they took to complete relative to the
            other items in the pool.
        
        total:
            Optional. Specify the total number of tasks, for progress reporting.
            Not necessary if your iterable defines __len__.
        
        initial:
            Optional. Specify a starting value for the progress bar.
        
        starmap:
            If True, each item should be a tuple, which will be unpacked into
             the arguments to the given function, like ``itertools.starmap()``.

        show_progress:
            If True, show a progress bar.
            By default, only show a progress bar if ``iterable`` has more than one element.

        pool_kwargs:
            keyword arguments to pass to the underlying Pool object,
            such as ``initializer`` or ``maxtasksperchild``.
    """
    assert bool(threads) ^ bool(processes), \
        "Specify either threads or processes (not both)"

    if threads:
        pool = ThreadPool(threads, **pool_kwargs)
    elif processes:
        pool = Pool(processes, **pool_kwargs)

    if total is None and hasattr(iterable, '__len__'):
        total = len(iterable)

    if ordered:
        f_map = pool.imap
    else:
        f_map = pool.imap_unordered
    
    if starmap:
        func = partial(apply_star, func)

    if show_progress is None:
        if hasattr(iterable, '__len__') and len(iterable) == 1:
            show_progress = False
        else:
            show_progress = True

    with pool:
        items = f_map(func, iterable, chunksize)
        items_progress = tqdm_proxy(items, initial=initial, total=total, leave=leave_progress, disable=not show_progress)
        items = list(items_progress)
        items_progress.close()
        return items


def apply_star(func, arg):
    return func(*arg)


DEFAULT_TIMESTAMP = datetime.strptime('2018-01-01 00:00:00', '%Y-%m-%d %H:%M:%S')
def parse_timestamp(ts, default=DEFAULT_TIMESTAMP):
    """
    Parse the given timestamp as a datetime object.
    If it is already a datetime object, it will be returned as-is.
    If it is None, then the given default timestamp will be returned.
    
    Acceptable formats are:

        2018-01-01             (date only)
        2018-01-01 00:00       (date and time)
        2018-01-01 00:00:00    (date and time with seconds)
        2018-01-01 00:00:00.0  (date and time with microseconds)
    
    Returns:
        datetime
    
    """
    if ts is None:
        ts = copy.copy(default)

    if isinstance(ts, datetime):
        return ts

    if isinstance(ts, str):
        if len(ts) == len('2018-01-01'):
            ts = datetime.strptime(ts, '%Y-%m-%d')
        elif len(ts) == len('2018-01-01 00:00'):
            ts = datetime.strptime(ts, '%Y-%m-%d %H:%M')
        elif len(ts) == len('2018-01-01 00:00:00'):
            ts = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        elif len(ts) >= len('2018-01-01 00:00:00.0'):
            frac = ts.split('.')[1]
            zero_pad = 6 - len(frac)
            ts += '0'*zero_pad
            ts = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S.%f')
        else:
            raise AssertionError("Bad timestamp format")

    return ts


def closest_approach(sv_vol, id_a, id_b, check_present=True):
    """
    Given a segmentation volume and two label IDs which it contains,
    find the two coordinates within id_a and id_b, respectively,
    which mark the two objects' closest approach, i.e. where the objects
    come closest to touching, even if they don't actually touch.
    
    Returns (coord_a, coord_b, distance)
    """
    assert id_a != 0 and id_b != 0, \
        "Can't use label 0 as an object ID in closest_approach()"
    
    assert sv_vol.dtype not in (np.uint64, np.int64, np.int32), \
        f"Volume type {sv_vol.dtype} is not convertible to uint32 without precision loss"
    
    mask_a = (sv_vol == id_a)
    mask_b = (sv_vol == id_b)

    if check_present and (not mask_a.any() or not mask_b.any()):
        # If either object is not present, there is no closest approach
        return (-1,-1,-1), (-1,-1,-1), np.inf
    
    if id_a == id_b:
        # IDs are identical.  Choose an arbitrary point.
        first_point = tuple(np.transpose(mask_a.nonzero())[0])
        return first_point, first_point, 0.0

    return closest_approach_between_masks(mask_a, mask_b)


def closest_approach_between_masks(mask_a, mask_b):
    """
    Given two non-overlapping binary masks,
    find the two coordinates within mask_a and mask_b, respectively,
    which mark the two objects' closest approach, i.e. where the objects
    come closest to touching, even if they don't actually touch.
    """
    # Wrapper function just for visibility to profilers
    def vectorDistanceTransform(mask):
        mask = mask.astype(np.uint32)
        mask = vigra.taggedView(mask, 'zyx'[-mask.ndim:])

        # vigra always returns the vectors (in the channel dimension)
        # in 'xyz' order, but we want zyx order!
        vdt = vigra.filters.vectorDistanceTransform(mask)
        vdt = vdt[..., ::-1]
        return vdt

    # For all voxels, find the shortest vector toward id_b
    to_b_vectors = vectorDistanceTransform(mask_b)

    # Magnitude of those vectors == distance to id_b
    to_b_distances = np.linalg.norm(to_b_vectors, axis=-1)

    # We're only interested in the voxels within id_a;
    # everything else is infinite distance
    to_b_distances[~mask_a] = np.inf

    # Find the point within id_a with the smallest vector
    point_a = np.unravel_index(np.argmin(to_b_distances), to_b_distances.shape)
    point_a = tuple(np.array(point_a, np.int32))

    # Its closest point id_b is indicated by the corresponding vector
    point_b = tuple((point_a + to_b_vectors[point_a]).astype(np.int32))

    return (point_a, point_b, to_b_distances[point_a])


def approximate_closest_approach(vol, id_a, id_b, scale=1):
    """
    Like closest_approach(), but first downsamples the data (for speed).
    
    The returned coordinates may not be precisely what closest_approach would have returned,
    but they are still guaranteed to reside within the objects of interest.
    """
    mask_a = (vol == id_a)
    mask_b = (vol == id_b)

    if not mask_a.any() or not mask_b.any():
        return np.inf

    scaled_mask_a, _ = downsample_binary_3d_suppress_zero(mask_a, (2**scale))
    scaled_mask_b, _ = downsample_binary_3d_suppress_zero(mask_b, (2**scale))

    scaled_point_a, scaled_point_b, _ = closest_approach_between_masks(scaled_mask_a, scaled_mask_b)

    scaled_point_a = np.asarray(scaled_point_a)
    scaled_point_b = np.asarray(scaled_point_b)

    # Compute the full-res box that corresponds to the downsampled points
    point_box_a = np.array([scaled_point_a, 1+scaled_point_a]) * (2**scale)
    point_box_b = np.array([scaled_point_b, 1+scaled_point_b]) * (2**scale)
    
    point_box_a = box_intersection(point_box_a, [(0,0,0), vol.shape])
    point_box_b = box_intersection(point_box_b, [(0,0,0), vol.shape])

    # Select the first non-zero point in the full-res box
    point_a = np.transpose(extract_subvol(mask_a, point_box_a).nonzero())[0] + point_box_a[0]
    point_b = np.transpose(extract_subvol(mask_b, point_box_b).nonzero())[0] + point_box_b[0]

    distance = np.linalg.norm(point_b - point_a)
    return (tuple(point_a), tuple(point_b), distance)


def upsample(orig_data, upsample_factor):
    """
    Upsample the given array by duplicating every
    voxel into the corresponding upsampled voxels.
    """
    orig_shape = np.array(orig_data.shape)
    upsampled_data = np.empty( orig_shape * upsample_factor, dtype=orig_data.dtype )
    v = view_as_blocks(upsampled_data, orig_data.ndim*(upsample_factor,))
    
    slicing = (Ellipsis,) + (None,)*orig_data.ndim
    v[:] = orig_data[slicing]
    return upsampled_data


def downsample_mask(mask, factor, method='or'):
    """
    Downsample a boolean mask by the given factor.
    """
    assert method in ('or', 'and')

    mask = np.asarray(mask)
    if not isinstance(factor, Iterable):
        factor = mask.ndim*(factor,)
    
    factor = np.asarray(factor)
    assert (factor >= 1).all()
    assert not any(mask.shape % factor), \
        "mask shape must be divisible by the downsampling factor"

    if (factor == 1).all():
        return mask

    mask = np.asarray(mask, order='C')
    v = view_as_blocks(mask, (*factor,))
    last_axes = (*range(v.ndim),)[-mask.ndim:]
    
    if method == 'or':
        f = np.logical_or.reduce
    if method == 'and':
        f = np.logical_and.reduce
    
    return f(v, axis=last_axes)


def extract_labels_from_volume(points_df, volume, box_zyx=None, vol_scale=0, label_names=None):
    """
    Given a list of point coordinates and a label volume, assign a
    label to each point based on its position in the volume.
    
    Extracting values from an array in numpy is simple.
    In the simplest case, this is equivalent to:
    
        coords = points_df[['z', 'y', 'x']].values.transpose()
        points_df['label'] = volume[(*coords,)]

    But this function supports extra features:
    
    - Points outside the volume extents are handled gracefully (they remain unlabeled).
    - The volume can be offset from the origin (doesn't start at (0,0,0)).
    - The volume can be provided in downscaled form, in which case the
      given points will be downscaled before sampling is performed.
    - Both label values (ints) and label names are output, if the label names were specified.
    
    Args:
        points_df:
            DataFrame with at least columns ['x', 'y', 'z'].
            The points in this DataFrame should be provided at SCALE-0,
            regardless of vol_scale.
            This function appends two additional columns to the DataFrame, IN-PLACE.
        
        volume:
            3D ndarray of label voxels
        
        box_zyx:
            The (min,max) coordinates in which the volume resides in the point coordinate space.
            It is assumed that this box is provided at the same scale as vol_scale,
            (i.e. it is not necessarily given using scale-0 coordiantes).
        
        vol_scale:
            Specifies the scale at which volume (and box_zyx) were provided.
            The coordinates in points_df will be downscaled accordingly.
            
        label_names:
            Optional.  Specifies how label IDs map to label names.
            If provided, a new column 'label_name' will be appended to
            points_df in addition to the 'label' column.

            Must be either:
            - a mapping of `{ label_id: name }` (or `{ name : label_id }`),
              indicating each label ID in the output image, or
            - a list label names in which case the mapping is determined automatically
              by enumerating the labels in the given order (starting at 1).
    
    Returns:
        None.  Results are appended to the points_df as new column(s).
    """
    if box_zyx is None:
        box_zyx = np.array(([0]*volume.ndim, volume.shape))

    assert ((box_zyx[1] - box_zyx[0]) == volume.shape).all() 

    assert points_df.index.duplicated().sum() == 0, \
        "This function doesn't work if the input DataFrame's index has duplicate values."

    downsampled_coords_zyx = (points_df[['z', 'y', 'x']] // (2**vol_scale)).astype(np.int32)

    # Drop everything outside the combined_box
    min_z, min_y, min_x = box_zyx[0] #@UnusedVariable
    max_z, max_y, max_x = box_zyx[1] #@UnusedVariable
    dc = downsampled_coords_zyx
    downsampled_coords_zyx = dc.loc[   (dc['z'] >= min_z) & (dc['z'] < max_z)
                                     & (dc['y'] >= min_y) & (dc['y'] < max_y)
                                     & (dc['x'] >= min_x) & (dc['x'] < max_x) ]
    del dc

    logger.info(f"Extracting labels from volume at {len(downsampled_coords_zyx)} points")
    downsampled_coords_zyx -= box_zyx[0]

    points_df.drop(columns=['label', 'label_name'], errors='ignore', inplace=True)
    points_df['label'] = volume.dtype.type(0)
    points_df.loc[downsampled_coords_zyx.index, 'label'] = volume[tuple(downsampled_coords_zyx.values.transpose())]

    if label_names is not None:
        if isinstance(label_names, Mapping):
            # We need a mapping of label_ids -> names.
            # If the user provided the reverse mapping,
            # then flip it.
            (k,v) = next(iter(label_names.items()))
            if isinstance(k, str):
                # Reverse the mapping
                label_names = { v:k for k,v in label_names.items() }
        else:
            label_names = dict(enumerate(label_names, start=1))
        
        name_set = ['<unspecified>', *label_names.values()]
        default_names = ['<unspecified>']*len(points_df)
        # FIXME: More than half of the runtime of this function is spent on this line!
        #        Is there some way to speed this up?
        points_df['label_name'] = pd.Categorical( default_names,
                                                  categories=name_set,
                                                  ordered=False )
        for label, name in label_names.items():
            rows = points_df['label'] == label
            points_df.loc[rows, 'label_name'] = name


def compute_merges(orig_vol, agg_vol):
    """
    Given an original volume and another volume which was generated
    exclusively from merges of the original, recover the merge decisions
    that were made.  That is, give the list of merges in the original
    volume that could reconstruct the geometry of segments in the
    agglomerated volume.
    
    Args:
        orig_vol:
            label volume, original segmentation

        agg_vol:
            label volume, agglomerated segmentation
    
    Returns:
        dict: { agg_id: [orig_id, orig_id, ...] },
        where the original IDs present in each merge are listed from largest to smallest.
        Agglomerated segments that exactly match an original segment (no merges) are not
        included in the results. (All lists in the results have at least two items.)
    
    Notes:
      - This function does not make any attempt to handle splits gracefully.
        For correct results, the every segment in the original volume should
        be a subset of only one segment in the agglomerated volume.
    
      - The label IDs in the agglomerated volume need not be related
        in any way to the label IDs in the original.
    """
    # Compute the set of unique orig-agg pairs, and the size of each
    df = pd.DataFrame({'orig': orig_vol.reshape(-1), 'agg': agg_vol.reshape(-1)})
    paired_seg_voxels = df.groupby(['orig', 'agg']).size().rename('voxels')
    paired_seg_voxels = pd.DataFrame(paired_seg_voxels)

    # For each agg ID with more than one corresponding 'orig' ID,
    # Compute the list of merges that reconstruct the agg geometry
    merges = {}    
    for agg, g_df in paired_seg_voxels.groupby('agg'):
        if len(g_df) > 1:
            merged_orig = g_df.sort_values('voxels', ascending=False).index.get_level_values('orig')
            merges[agg] = merged_orig.tolist()

    return merges


def unordered_duplicated(df, subset=None, keep='first'):
    """
    Like pd.DataFrame.duplicated(), but sorts each row first, so
    rows can be considered duplicates even if their values don't
    appear in the same order.

    Example:
    
        >>> df = pd.DataFrame( [(1, 2, 0.0),
                                (2, 1, 0.1), # <-- duplicate a/b columns
                                (3, 4, 0.2)],
                              columns=['a', 'b', 'score'])

        >>> unordered_duplicated(df, ['a', 'b'])
        0    False
        1     True
        2    False
        dtype: bool    
    """
    if subset is None:
        subset = list(df.columns)
    normalized_cols = np.sort(df[subset].values, axis=1)
    dupes = pd.DataFrame(normalized_cols).duplicated(keep=keep).values
    return pd.Series(dupes, index=df.index)


def drop_unordered_duplicates(df, subset=None, keep='first'):
    """
    Like pd.DataFrame.drop_duplicates(), but sorts each row first, so
    rows can be considered duplicates even if their values don't
    appear in the same order.

    Example:

        >>> df = pd.DataFrame( [(1, 2, 0.0),
                                (2, 1, 0.1), # <-- duplicate a/b columns
                                (3, 4, 0.2)],
                              columns=['a', 'b', 'score'])

        >>> drop_unordered_duplicates(df, ['a', 'b'])
           a  b  score
        0  1  2    0.0
        2  3  4    0.2

    """
    dupes = unordered_duplicated(df, subset, keep)
    return df.loc[~dupes]


def swap_df_cols(df, prefixes=None, swap_rows=None, suffixes=['_a', '_b']):
    """
    Swap selected columns of a dataframe, specified as a list of prefixes and two suffixes.
    Operates IN-PLACE, but incurs a full copy internally of the selected columns.
    
    Args:
        df:
            Input dataframe, with columns to be swapped.
        
        prefixes:
            columns to swap, minus their suffixes.
            If not provided, all columns with corresponding suffixes will be swapped.
        
        swap_rows:
            Optional.
            Specify a subset of rows in the dataframe to apply the swap to.
            Should be a Series boolean values, or a list of index values. 
            If this is a Series, it must have the same index as the input dataframe.
            If not provided, all rows are swapped.
        
        suffixes:
            Used to identify the left/right columns of each swapped pair.
        
    Returns:
        None.  Operates IN-PLACE.
    
    Example:
        >>> df = pd.DataFrame(np.arange(12).reshape(3,4), columns=['x_a', 'x_b', 'y_a', 'y_b'])

        >>> df
           x_a  x_b  y_a  y_b
        0    0    1    2    3
        1    4    5    6    7
        2    8    9   10   11

        >>> swap_df_cols(df, None, [True, False, True])
           x_a  x_b  y_a  y_b
        0    1    0    3    2
        1    4    5    6    7
        2    9    8   11   10
                    
    """
    assert len(suffixes) == 2

    if prefixes is None:
        prefixes = set()
        suffix_len = len(suffixes[0])
        assert suffix_len == len(suffixes[1]), "Suffixes are not the same length"
        for col in df.columns:
            prefix = col[:-suffix_len]
            if (prefix + suffixes[0] in df) and (prefix + suffixes[1] in df):
                prefixes.add(prefix)
        assert prefixes, "Could not find any column pairs with the given suffixes"

    if swap_rows is None:
        swap_rows = slice(None)
    else:
        assert swap_rows.dtype == np.bool

    all_cols = [p + s for p,s in product(prefixes, suffixes)]
    missing_cols = set(all_cols) - set(df.columns)
    assert not missing_cols, \
        f"The following columns do not exist in the input DataFrame: {list(missing_cols)}"

    orig_df = df[all_cols].copy()

    for prefix in prefixes:
        col_a = prefix + suffixes[0]
        col_b = prefix + suffixes[1]
        df.loc[swap_rows, col_a] = orig_df.loc[swap_rows, col_b]
        df.loc[swap_rows, col_b] = orig_df.loc[swap_rows, col_a]


def tqdm_proxy(iterable=None, *, logger=None, level=logging.INFO, **kwargs):
    """
    Useful as an (almost) drop-in replacement for ``tqdm`` which can be used
    in EITHER an interactive console OR a script that logs to file.

    Automatically detects whether or not sys.stdout is a file or a console,
    and configures tqdm accordingly.

    - If your code is running from an interactive console, this acts like plain ``tqdm``.
    - If your code is running from an ipython notebook, this acts like ``tqdm.notebook.tqdm``.
    - If your code is running from a batch script (i.e. printing to a log file, not the console),
      this code uses the supplied logger to periodically output a textual progress bar.
      If no logger is supplied, a logger is automatically created using the name of
      the calling module.

    Example:

        for i in tqdm_proxy(range(1000)):
            # do some stuff

    Note for JupyterLab users:

        If you get errors in this function, you need to run the following commands:

            conda install -c conda-forge ipywidgets
            jupyter nbextension enable --py widgetsnbextension
            jupyter labextension install @jupyter-widgets/jupyterlab-manager

        ...and then reload your jupyterlab session, and restart your kernel.
    """
    assert 'file' not in kwargs, \
        "There's no reason to use this function if you are providing your own output stream"

    # Special case for tqdm_proxy(range(...))
    if iterable is not None and isinstance(iterable, range) and 'total' not in kwargs:
        kwargs['total'] = (iterable.stop - iterable.start) // iterable.step

    try:
        import ipykernel.iostream
        from tqdm.notebook import tqdm as tqdm_notebook
        if isinstance(sys.stdout, ipykernel.iostream.OutStream):
            return tqdm_notebook(iterable, **kwargs)
    except ImportError:
        pass

    _tqdm = tqdm
    _file = None
    disable_monitor = False

    if not _file and os.isatty(sys.stdout.fileno()):
        _file = sys.stdout
    else:
        if logger is None:
            frame = inspect.stack()[1]
            modname = inspect.getmodulename(frame[1])
            if modname:
                logger = logging.getLogger(modname)
            else:
                logger = logging.getLogger("unknown")

        _file = TqdmToLogger(logger, level)

        # The tqdm monitor thread messes up our 'miniters' setting, so disable it.
        disable_monitor = True

        if 'ncols' not in kwargs:
            kwargs['ncols'] = 100

        if 'miniters' not in kwargs:
            # Aim for 5% updates
            if 'total' in kwargs:
                kwargs['miniters'] = kwargs['total'] // 20
            elif hasattr(iterable, '__len__'):
                kwargs['miniters'] = len(iterable) // 20


    kwargs['file'] = _file
    bar = _tqdm(iterable, **kwargs)
    if disable_monitor:
        bar.monitor_interval = 0
    return bar


class TqdmToLogger(io.StringIO):
    """
    Output stream for tqdm which will output to logger module instead of stdout.
    Copied from:
    https://github.com/tqdm/tqdm/issues/313#issuecomment-267959111
    """
    logger = None
    level = logging.INFO
    buf = ''

    def __init__(self, logger, level=logging.INFO):
        super().__init__()
        self.logger = logger
        self.level = level

    def write(self,buf):
        self.buf = buf.strip('\r\n\t ')

    def flush(self):
        self.logger.log(self.level, self.buf)


@jit(nopython=True, nogil=True)
def encode_coords_to_uint64(coords):
    """
    Encode an array of (N,3) int32 into an array of (N,) uint64,
    giving 21 bits per coord (20 bits plus a sign bit for each).
    
    FIXME: As it stands right now, this function doesn't work
           properly for negative coordinates.
           This should return int64, anyway.  
    """
    assert coords.shape[1] == 3
    
    N = len(coords)
    encoded_coords = np.empty(N, np.uint64)

    for i in range(N):
        z, y, x = coords[i]
        encoded = np.uint64(0)
        encoded |= np.uint64(z) << 42
        encoded |= np.uint64(y) << 21
        encoded |= np.uint64(x)
        encoded_coords[i] = encoded

    return encoded_coords


@jit(nopython=True, nogil=True)
def decode_coords_from_uint64(encoded_coords):
    """
    The reciprocal to encoded_coords_to_uint64(), above.
    """
    N = len(encoded_coords)
    coords = np.empty((N,3), np.int32)
    
    for i in range(N):
        encoded = encoded_coords[i]
        z = np.int32((encoded >> 2*21) & 0x1F_FFFF) # 21 bits
        y = np.int32((encoded >>   21) & 0x1F_FFFF) # 21 bits
        x = np.int32((encoded >>    0) & 0x1F_FFFF) # 21 bits
        
        # Check sign bits and extend if necessary
        if encoded & (1 << (3*21-1)):
            z |= np.int32(0xFFFF_FFFF << 21)
    
        if encoded & (1 << (21*2-1)):
            y |= np.int32(0xFFFF_FFFF << 21)
    
        if encoded & (1 << (21*1-1)):
            x |= np.int32(0xFFFF_FFFF << 21)
        
        coords[i] = (z,y,x)

    return coords


def mask_centroid(mask, as_int=False):
    """
    Compute the centroid of an ND mask.
    Requires N passes but not much RAM overhead.
    """
    # Use broadcasting tricks to avoid creating a full field of coordinates
    # When implicitly broadcasted with the 'where' arg below,
    # the operation sums over all coordinates that belong to a non-zero voxel.
    mask = mask.astype(bool, copy=False)
    slicing = tuple(slice(None, s) for s in mask.shape)
    coords = np.ogrid[slicing]

    size = mask.sum()
    centroid = []
    for a in coords:
        c = np.add.reduce(a, axis=None, where=mask) / size
        centroid.append(c)

    centroid = np.array(centroid)
    if as_int:
        return centroid.astype(np.int32)
    else:
        return centroid


@lru_cache(maxsize=1)
def sphere_mask(radius):
    """
    Return the binary mask of a sphere.
    Resulting array is a cube with side 2R+1
    """
    r = radius
    cz, cy, cx = np.ogrid[-r:r+1, -r:r+1, -r:r+1]
    distances_sq = cz**2 + cy**2 + cx**2
    mask = (distances_sq <= r**2)

    # The result will be cached, so don't let the caller overwrite it!
    mask.flags['WRITEABLE'] = False
    return mask


@lru_cache(maxsize=1)
def ellipsoid_mask(rz, ry, rx):
    """
    Return the binary mask of an axis-aligned ellipsoid.
    Resulting array has dimensions (2*rz+1, 2*ry+1, 2*rx+1)
    """
    cz, cy, cx = np.ogrid[-rz:rz+1, -ry:ry+1, -rx:rx+1]
    k = (cz/rz)**2 + (cy/ry)**2 + (cx/rx)**2
    mask = (k <= 1)

    # The result will be cached, so don't let the caller overwrite it!
    mask.flags['WRITEABLE'] = False
    return mask


def perform_bigquery(q, client=None, project='janelia-flyem'):
    """
    Send the given SQL query to BigQuery
    and return the results as a DataFrame.
    """
    from google.cloud import bigquery
    assert 'GOOGLE_APPLICATION_CREDENTIALS' in os.environ

    if client is None:
        assert project in os.environ['GOOGLE_APPLICATION_CREDENTIALS'], \
            "Usually the credentials file name mentions the project name.  It looks like you have the wrong credentials loaded."
        client = bigquery.Client(project)

    # In theory, there are faster ways to download table data using parquet,
    # but bigquery keeps giving me errors when I try that.
    r = client.query(q).result()
    return r.to_dataframe()
