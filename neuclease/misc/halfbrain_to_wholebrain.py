import logging
from functools import wraps

import numpy as np
import pandas as pd
from requests import HTTPError

from neuclease.util import Timer, tqdm_proxy as tqdm
from neuclease.dvid import fetch_sparsevol, fetch_labelmap_specificblocks
from neuclease.dvid.rle import blockwise_masks_from_ranges


class FindHBWBOverlaps:
    def __init__(self, halfbrain_seg=None, brain_seg=None):
        self.halfbrain_seg = halfbrain_seg
        self.brain_seg = brain_seg
        if halfbrain_seg is None:
            self.halfbrain_seg = (
                'http://emdata6.int.janelia.org:8000',
                'eaef6946397647d3a6889568c2f96f4b',
                'segmentation'
            )

        self.brain_seg = brain_seg
        if brain_seg is None:
            self.brain_seg = (
                'http://emdata6.int.janelia.org:8510',
                '138069aba8e94612b37d119778a89a1c',
                'segmentation'
            )

    def find_hbwb_overlaps(self, halfbrain_body, scale=0, threshold_frac=0.05, show_progress=True):
        """
        For a given body in our half-brain segmentation, determine
        which bodies it overlaps with in out whole-brain segmentation.

        Returns a table of whole-brain body IDs along with the count of
        overlapping voxels and fraction of the original body.

        Note:
            The results will include body 0 if the body's mask overlaps
            with label 0 in the whole-brain segmentation.
        """
        HALFBRAIN_LAST_TAB_X = (35202 // 64) * 64 // 2**scale

        if show_progress:
            log_level = logging.INFO
        else:
            log_level = logging.DEBUG

        def empty_counts():
            """Return an empty dataframe with the appropriate columns."""
            empty_index = pd.Index(np.array([], dtype=np.uint64), name='brain_body')
            counts = pd.Series([], name='count', dtype=np.int64, index=empty_index)
            df = counts.to_frame()
            df['halfbrain_frac'] = 0.0
            df['halfbrain_body'] = halfbrain_body
            return df.reset_index()[['halfbrain_body', 'brain_body', 'count', 'halfbrain_frac']]

        with Timer(f"Body {halfbrain_body}: Fetching sparsevol", None, log_level):
            try:
                rng = fetch_sparsevol(*self.halfbrain_seg, halfbrain_body, scale=scale, format='ranges')
                body_size = (rng[:, 3] - rng[:, 2] + 1).sum()
            except HTTPError:
                return empty_counts()

        with Timer(f"Body {halfbrain_body}: Setting up masks", None, log_level):
            boxes, masks = blockwise_masks_from_ranges(rng, (64,64,64))

        if (boxes[:, 0, :] > HALFBRAIN_LAST_TAB_X).all():
            # Fast path for objects that lie completely within the taboo region.
            return empty_counts()

        with Timer(f"Body {halfbrain_body}: Fetching specificblocks", None, log_level):
            brain_corners = 4096 // (2**scale) + boxes[:, 0, :]
            seg_dict = fetch_labelmap_specificblocks(*self.brain_seg, brain_corners, scale=scale, format='callable-blocks')
            assert len(boxes) == len(seg_dict), \
                f"Body {halfbrain_body}: Mismatch between masks and seg: {len(boxes)} != {len(seg_dict)}"

        with Timer(f"Body {halfbrain_body}: Counting voxels", None, log_level):
            block_counts = []
            seg_items = seg_dict.items()
            mask_items = zip(brain_corners, masks)
            for (mask_corner, mask), (seg_corner, compressed_seg) in tqdm(zip(mask_items, seg_items), disable=not show_progress, total=len(seg_items)):
                assert (mask_corner == seg_corner).all(), \
                    f"Body {halfbrain_body}: Mask corner doesn't match seg_corner: {mask_corner} != {seg_corner}"
                if mask_corner[0] >= HALFBRAIN_LAST_TAB_X:
                    continue

                OUT_OF_MASK = 2**63
                seg = np.where(mask, compressed_seg(), OUT_OF_MASK)
                vc = pd.value_counts(seg.ravel()).rename_axis('brain_body').rename('count')
                vc = vc[vc.index != OUT_OF_MASK]
                block_counts.append(vc)

        if len(block_counts) == 0:
            return empty_counts()

        counts = pd.concat(block_counts).groupby(level=0).sum()
        counts.sort_values(ascending=False, inplace=True)
        df = counts.to_frame()
        df['halfbrain_frac'] = df['count'] / body_size
        df = df[df['halfbrain_frac'] >= threshold_frac]
        df['halfbrain_body'] = halfbrain_body
        return df.reset_index()[['halfbrain_body', 'brain_body', 'count', 'halfbrain_frac']]

    @wraps(find_hbwb_overlaps)
    def __call__(self, *args, **kwargs):
        return self.find_hbwb_overlaps(*args, **kwargs)


find_hbwb_overlaps = FindHBWBOverlaps()
