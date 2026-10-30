from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import time

SPEEDSCOPE_SCHEMA = "https://www.speedscope.app/file-format-schema.json"
DEFAULT_WINDOW_SECONDS = 30 * 60
# while calls are arriving, old ones are dropped at most this often
PRUNE_INTERVAL_MS = 1000

def now_ms() -> int:
    return time.perf_counter_ns() // 1_000_000

def normalize_message(message: str | None) -> str:
    return " ".join(message.split()) if message else ""

def hash_message(message: str) -> int:
    """Stable 64-bit hash of a normalized message; blank messages hash to 0."""
    if not message:
        return 0
    return int.from_bytes(hashlib.blake2b(message.encode(), digest_size=8).digest(), "big")

@dataclass
class ProfFrame:
    object:str
    function:str
    message_hash:int
    pid:int
    tenant_id:int
    time:int = 0
    total_time:int = 0
    closed:bool = False
    children: list[ProfFrame] = field(default_factory=list, repr=False)
    parent: ProfFrame | None = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.object, self.function, self.message_hash)

@dataclass
class FrameRecorder:
    stacked_frames: dict[int, ProfFrame | None] = field(default_factory=dict)
    root_frames: dict[int, list[ProfFrame]] = field(default_factory=dict)
    messages: dict[int, str] = field(default_factory=lambda: {0: ""})
    # how many recorded frames use each message, so a message is dropped with its last frame
    message_refs: dict[int, int] = field(default_factory=dict)
    # top-level calls that ended longer ago than this are dropped, along with everything under them
    window_ms: int = DEFAULT_WINDOW_SECONDS * 1000
    # bumped on every recorded open or close, and when old calls are dropped,
    # so viewers can skip reloading when nothing changed
    version: int = 0
    _next_prune: int = 0

    def open_call(self, object, function, message, pid, tenant_id, at: int | None = None):
        message = normalize_message(message)
        message_hash = hash_message(message)
        self.messages.setdefault(message_hash, message)
        if message_hash:
            self.message_refs[message_hash] = self.message_refs.get(message_hash, 0) + 1
        new_frame = ProfFrame(object, function, message_hash, pid, tenant_id)
        parent = self.stacked_frames.get(pid)
        if parent:
            new_frame.parent = parent
            parent.children.append(new_frame)
        else:
            self.root_frames.setdefault(pid, []).append(new_frame)
        new_frame.time = now_ms() if at is None else at
        self.stacked_frames[pid] = new_frame
        self.version += 1
        if new_frame.time >= self._next_prune:
            self._next_prune = new_frame.time + PRUNE_INTERVAL_MS
            self.prune(new_frame.time)

    def close_call(self, object, function, pid, at: int | None = None):
        """Close the most recent open call on pid matching object.function.
        Calls opened inside it that are still open lost their own close (e.g. a
        dropped line), so they're closed at the same time. A close that matches
        nothing open is ignored."""
        target = self.stacked_frames.get(pid)
        while target and (target.object, target.function) != (object, function):
            target = target.parent
        if not target:
            return
        end = now_ms() if at is None else at
        frame = self.stacked_frames[pid]
        while True:
            frame.total_time = end - frame.time
            frame.closed = True
            if frame is target:
                break
            frame = frame.parent
        self.stacked_frames[pid] = target.parent
        self.version += 1

    def prune(self, now: int | None = None) -> None:
        """Drop top-level calls that ended more than window_ms before now.
        A pid's roots are in start order and only its last one can still be open,
        so each pid is trimmed from the front."""
        cutoff = (now_ms() if now is None else now) - self.window_ms
        dropped = False
        for pid in list(self.root_frames):
            roots = self.root_frames[pid]
            keep = 0
            while keep < len(roots) and roots[keep].closed and roots[keep].time + roots[keep].total_time < cutoff:
                self._forget(roots[keep])
                keep += 1
            if keep:
                dropped = True
                del roots[:keep]
            if not roots:
                del self.root_frames[pid]
                self.stacked_frames.pop(pid, None)
        if dropped:
            self.version += 1

    def _forget(self, root: ProfFrame) -> None:
        """Release the messages used by root and everything under it."""
        stack = [root]
        while stack:
            frame = stack.pop()
            stack.extend(frame.children)
            if not frame.message_hash:
                continue
            refs = self.message_refs[frame.message_hash] - 1
            if refs:
                self.message_refs[frame.message_hash] = refs
            else:
                del self.message_refs[frame.message_hash]
                del self.messages[frame.message_hash]

    def lookup_message(self, message_hash: int) -> str | None:
        return self.messages.get(message_hash)

    def tenant_ids(self) -> list[int]:
        self.prune()
        return sorted({root.tenant_id for roots in self.root_frames.values() for root in roots})

    def to_speedscope(self, name="pipeflame", tenant_id: int | None = None) -> dict:
        """Build a speedscope file with one evented profile per pid, picked from
        speedscope's profile list. Every profile shares one time axis starting at the
        earliest call shown, so flipping between pids keeps times comparable.
        A pid with no calls left in the window has no profile. Frames still open are
        closed at the current time. tenant_id limits it to that tenant's top-level calls."""
        frames: list[dict] = []
        frame_index: dict[tuple, int] = {}
        end_now = now_ms()
        self.prune(end_now)

        def index_of(key: tuple, entry: dict) -> int:
            if key not in frame_index:
                frame_index[key] = len(frames)
                frames.append(entry)
            return frame_index[key]

        def frame_entry(frame: ProfFrame) -> dict:
            entry = {"name": f"{frame.object}.{frame.function}"}
            # message is usually a SQL query; it goes in `file` so it shows in the
            # detail panel without cluttering the bar label
            if frame.message_hash:
                entry["file"] = self.messages[frame.message_hash]
            return entry

        def emit(frame: ProfFrame, events: list[dict], base: int) -> int:
            idx = index_of(frame.key, frame_entry(frame))
            events.append({"type": "O", "frame": idx, "at": frame.time - base})
            end = (frame.time + frame.total_time if frame.closed else end_now) - base
            for child in frame.children:
                end = max(end, emit(child, events, base))
            events.append({"type": "C", "frame": idx, "at": end})
            return end

        shown: dict[int, list[ProfFrame]] = {}
        for pid in sorted(self.root_frames):
            roots = self.root_frames[pid]
            if tenant_id is not None:
                roots = [root for root in roots if root.tenant_id == tenant_id]
            if roots:
                shown[pid] = roots
        base = min((roots[0].time for roots in shown.values()), default=0)

        profiles: list[dict] = []
        for pid, roots in shown.items():
            events: list[dict] = []
            end = 0
            for root in roots:
                end = emit(root, events, base)
            # a pid serves whichever tenants queue into it, so it's named for the pid alone
            profiles.append({
                "type": "evented",
                "name": f"pid {pid}",
                "unit": "milliseconds",
                "startValue": 0,
                "endValue": end,
                "events": events,
            })
        # same axis for every pid, so switching profiles doesn't rescale time
        axis_end = max((profile["endValue"] for profile in profiles), default=0)
        for profile in profiles:
            profile["endValue"] = axis_end

        return {
            "$schema": SPEEDSCOPE_SCHEMA,
            "name": name,
            "activeProfileIndex": 0,
            "exporter": "pipeflame",
            "shared": {"frames": frames},
            "profiles": profiles,
        }
