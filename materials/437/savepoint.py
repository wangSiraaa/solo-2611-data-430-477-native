"""Savepoint serialization and re-partitioning.

A savepoint is a single JSON document containing, for every one of the fixed
16 key-groups, the complete :class:`~state.GroupState` snapshot: key values,
not-yet-fired timers, current watermark and completed-output identities - plus
the durable, re-readable result store keyed by timer_id.  Timers are ordinary
state, never thread-local facilities, so they travel with their group.

Writes are staged: bytes first go to a unique temporary file in the same
directory and are only published with ``os.replace`` (atomic on POSIX).  A
half-written file therefore can never be selected as a restore point, and if a
re-partition fails the previous committed version stays byte-for-byte intact.
"""

import json
import os
import tempfile

from state import GroupState

SAVEPOINT_VERSION = 1
KEY_GROUP_COUNT = 16


def stage(states, result_snapshot, path, epoch):
    """Write a new savepoint to ``path.tmp`` without touching ``path``.

    Returns the temporary path.  Call :func:`commit` to publish it; if anything
    fails before the commit the old ``path`` remains the usable version.
    """
    groups = {}
    for st in states:
        if st.group in groups:
            raise ValueError("duplicate group %d in savepoint input" % st.group)
        groups[st.group] = st.snapshot()
    if set(groups) != set(range(KEY_GROUP_COUNT)):
        raise ValueError(
            "savepoint must cover groups 0..%d, got %r"
            % (KEY_GROUP_COUNT - 1, sorted(groups)))

    doc = {
        'version': SAVEPOINT_VERSION,
        'key_group_count': KEY_GROUP_COUNT,
        'epoch': epoch,
        'groups': groups,
        # Re-readable result store: timer_id -> full output record.  Keeping
        # the completed identities in the group state *and* the result records
        # here means a restored runtime can never hide a duplicate by wiping
        # the output list.
        'results': dict(result_snapshot),
    }
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.',
                               suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(doc, f, sort_keys=True, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def commit(tmp, path):
    """Atomically publish a staged savepoint."""
    os.replace(tmp, path)
    dirpath = os.path.dirname(os.path.abspath(path))
    try:
        dfd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(dfd)  # make the rename durable; best-effort otherwise
        finally:
            os.close(dfd)
    except OSError:
        pass  # os.replace itself is already atomic at the filesystem level


def save(states, result_snapshot, path, epoch):
    """Stage and publish in one step (used by planned rescales)."""
    commit(stage(states, result_snapshot, path, epoch), path)


def load(path):
    """Load and validate a savepoint, raising on any truncated/corrupt file."""
    with open(path) as f:
        doc = json.load(f)  # raises on a half-written document
    if not isinstance(doc, dict) or doc.get('version') != SAVEPOINT_VERSION:
        raise ValueError("unsupported or corrupt savepoint: %r" % path)
    if doc.get('key_group_count') != KEY_GROUP_COUNT:
        raise ValueError("key_group_count mismatch in %r" % path)
    raw_groups = doc['groups']
    if set(map(int, raw_groups)) != set(range(KEY_GROUP_COUNT)):
        raise ValueError("savepoint %r does not cover all groups" % path)
    groups = {}
    for gid, snap in raw_groups.items():
        st = GroupState.from_snapshot(snap)
        if st.group != int(gid):
            raise ValueError("group id mismatch inside savepoint %r" % path)
        # A restored timer must still be pending; a fired one lives in
        # completed, never in both maps.
        if set(st.timers) & set(st.completed):
            raise ValueError("timer present as pending and completed")
        groups[st.group] = st
    results = doc['results']
    all_completed = set().union(*[set(st.completed) for st in
                                  groups.values()])
    # Every stored result must correspond to a fired timer.  The reverse
    # inclusion is deliberate: a generation-fence handoff can leave a fired
    # timer whose late result was rejected, so its identity stays in
    # ``completed`` (blocking any duplicate re-fire) without a result record.
    if set(results) - all_completed:
        raise ValueError("result store contains unknown timer identities")
    return doc['epoch'], groups, results


def restore(path, workers):
    """Re-partition the 16 group states across ``workers`` real worker threads.

    Ownership is the same stable rule used while running:
    ``owner = group % workers``.  Each group ends up with exactly one owner and
    timer records move together with the group that owns their key.
    """
    if not 1 <= workers <= 4:
        raise ValueError("worker count must be in 1..4, got %d" % workers)
    _, groups, results = load(path)
    assigned = [[] for _ in range(workers)]
    distribution = {}
    for gid in range(KEY_GROUP_COUNT):
        owner = gid % workers
        assigned[owner].append(groups[gid])
        distribution[gid] = owner
    return assigned, dict(results), distribution
