from collections import Iterable

import networkx as nx

from ..util import uuids_match
from . import dvid_api_wrapper, fetch_generic_json

INSTANCE_TYPENAMES = """\
annotation
float32blk
googlevoxels
imagetile
keyvalue
labelarray
labelblk
labelgraph
labelmap
labelsz
labelvol
multichan16
rgba8blk
roi
tarsupervoxels
uint16blk
uint32blk
uint64blk
uint8blk
""".split()

@dvid_api_wrapper
def fetch_repo_info(server, uuid, *, session=None):
    return fetch_generic_json(f'http://{server}/api/repo/{uuid}/info', session=session)
    

@dvid_api_wrapper
def expand_uuid(server, uuid, repo_uuid=None, *, session=None):
    """
    Given an abbreviated uuid, find the matching uuid
    on the server and return the complete uuid.
    
    Args:
        server:
            dvid server
        uuid:
            Abbreviated uuid, e.g. `662edc`
        repo_uuid:
            The repo in which to search for the complete uuid.
            If not provided, the abbreviated uuid itself is used.
        
    Returns:
        Complete uuid, e.g. `662edcb44e69481ea529d89904b5ef9b`
    """
    repo_uuid = repo_uuid or uuid
    repo_info = fetch_repo_info(server, repo_uuid, session=session)
    full_uuids = repo_info["DAG"]["Nodes"].keys()
    
    matching_uuids = list(filter(lambda full_uuid: uuids_match(uuid, full_uuid), full_uuids))
    if len(matching_uuids) == 0:
        raise RuntimeError(f"No matching uuid for '{uuid}'")
    
    if len(matching_uuids) > 1:
        raise RuntimeError(f"Multiple ({len(matching_uuids)}) uuids match '{uuid}': {matching_uuids}")

    return matching_uuids[0]


@dvid_api_wrapper
def create_instance(server, uuid, instance, typename, versioned=True, compression=None, tags=[], type_specific_settings={}, *, session=None):
    """
    Create a data instance of the given type.
    
    Note:
        Some datatypes, such as labelmap or tarsupervoxels, have their own creation functions below,
        which are more convenient than calling this function directly.

    Args:
        typename:
            Valid instance names are listed in INSTANCE_TYPENAMES
        
        versioned:
            Whether or not the instance should be versioned.
    
        compression:
            Which compression DVID should use when storing the data in the instance.
            Different instance types support different compression options.
            Typical choices are: ['none', 'snappy', 'lz4', 'gzip'].
            
            Note: Here, the string 'none' means "use no compression",
                  whereas a Python None value means "Let DVID choose a default compression type".
    
        tags:
            Optional 'tags' to initialize the instance with, e.g. "type=meshes".
            
        type_specific_settings:
            Additional datatype-specific settings to send in the JSON body.
    """
    assert typename in INSTANCE_TYPENAMES, f"Unknown typename: {typename}"

    settings = {}
    settings["dataname"] = instance
    settings["typename"] = typename

    if not versioned:
        settings["versioned"] = 'false'
    
    if typename == 'tarsupervoxels':
        # Will DVID return an error for us in these cases?
        # If so, we can remove these asserts...
        assert not versioned, "Instances of tarsupervoxels must be unversioned"
        assert compression in (None, 'none'), "Compression not supported for tarsupervoxels"

    if compression is not None:
        assert compression in ('none', 'snappy', 'lz4', 'gzip') # jpeg is also supported, but then we need to parse e.g. jpeg:80
        settings["Compression"] = compression
    
    if tags:
        settings["Tags"] = ','.join(tags)
    
    settings.update(type_specific_settings)
    
    r = session.post(f"http://{server}/api/repo/{uuid}/instance", json=settings)
    r.raise_for_status()

@dvid_api_wrapper
def create_voxel_instance(server, uuid, instance, typename, versioned=True, compression=None, tags=[],
                          block_size=64, voxel_size=8.0, voxel_units='nanometers', background=None,
                          type_specific_settings={}, *, session=None):
    """
    Generic function ot create an instance of one of the voxel datatypes, such as uint8blk or labelmap.
    
    Note: For labelmap instances in particular, it's more convenient to call create_labelmap_instance().
    """
    assert typename in ("uint8blk", "uint16blk", "uint32blk", "uint64blk", "float32blk", "labelblk", "labelarray", "labelmap")

    if not isinstance(block_size, Iterable):
        block_size = 3*(block_size,)

    if not isinstance(voxel_size, Iterable):
        voxel_size = 3*(voxel_size,)

    block_size_str = ','.join(map(str, block_size))
    voxel_size_str = ','.join(map(str, voxel_size))

    type_specific_settings = dict(type_specific_settings)
    type_specific_settings["BlockSize"] = block_size_str
    type_specific_settings["VoxelSize"] = voxel_size_str
    type_specific_settings["VoxelUnits"] = voxel_units
    
    if background is not None:
        assert typename in ("uint8blk", "uint16blk", "uint32blk", "uint64blk", "float32blk"), \
            "Background value is only valid for block-based instance types."
        type_specific_settings["Background"] = background
    
    create_instance(server, uuid, instance, typename, versioned, compression, tags, type_specific_settings, session=session)


@dvid_api_wrapper
def fetch_and_parse_dag(server, repo_uuid, *, session=None):
    # FIXME: Better name would be 'fetch_repo_dag'
    """
    Read the /repo/info for the given repo UUID
    and extract the DAG structure from it.

    Return the DAG as a nx.DiGraph, whose nodes' attribute
    dicts contain the fields from the DAG json data.
    """
    repo_info = fetch_repo_info(server, repo_uuid, session=session)

    # The JSON response is a little weird.
    # The DAG nodes are given as a dict with uuids as keys,
    # but to define the DAG structure, parents and children are
    # referred to by their integer 'VersionID' (not their UUID).

    # Let's start by creating an easy lookup from VersionID -> node info
    node_infos = {}
    for node_info in repo_info["DAG"]["Nodes"].values():
        version_id = node_info["VersionID"]
        node_infos[version_id] = node_info
        
    g = nx.DiGraph()
    
    # Add graph nodes (with node info from the json as the nx node attributes)
    for version_id, node_info in node_infos.items():
        g.add_node(node_info["UUID"], **node_info)
        
    # Add edges from each parent to all children
    for version_id, node_info in node_infos.items():
        parent_uuid = node_info["UUID"]
        for child_version_id in node_info["Children"]:
            child_uuid = node_infos[child_version_id]["UUID"]
            g.add_edge(parent_uuid, child_uuid)

    return g

