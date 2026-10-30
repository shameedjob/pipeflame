from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import time

SPEEDSCOPE_SCHEMA = "https://www.speedscope.app/file-format-schema.json"

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

    def open_call(self, object, function, message, pid, tenant_id, at: int | None = None):
        message = normalize_message(message)
        message_hash = hash_message(message)
        self.messages.setdefault(message_hash, message)
        new_frame = ProfFrame(object, function, message_hash, pid, tenant_id)
        parent = self.stacked_frames.get(pid)
        if parent:
            new_frame.parent = parent
            parent.children.append(new_frame)
        else:
            self.root_frames.setdefault(pid, []).append(new_frame)
        new_frame.time = now_ms() if at is None else at
        self.stacked_frames[pid] = new_frame

    def close_call(self, object, function, pid, at: int | None = None):
        frame = self.stacked_frames.get(pid)
        if not frame or (frame.object, frame.function) != (object, function):
            return
        frame.total_time = (now_ms() if at is None else at) - frame.time
        frame.closed = True
        self.stacked_frames[pid] = frame.parent

    def lookup_message(self, message_hash: int) -> str | None:
        return self.messages.get(message_hash)

    def tenant_ids(self) -> list[int]:
        return sorted({root.tenant_id for roots in self.root_frames.values() for root in roots})

    def to_speedscope(self, name="pipeflame", tenant_id: int | None = None) -> dict:
        """Build a speedscope file with one evented profile holding every pid.
        Speedscope can't draw overlapping stacks, so each pid sits under its own
        "pid N" frame and the pids are laid out back to back; time within a pid is
        real, time between pids is not. Frames still open are closed at the current
        time. tenant_id limits it to that tenant's top-level calls."""
        frames: list[dict] = []
        frame_index: dict[tuple, int] = {}
        end_now = now_ms()

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

        def emit(frame: ProfFrame, events: list[dict], shift: int) -> int:
            idx = index_of(frame.key, frame_entry(frame))
            events.append({"type": "O", "frame": idx, "at": frame.time + shift})
            end = (frame.time + frame.total_time if frame.closed else end_now) + shift
            for child in frame.children:
                end = max(end, emit(child, events, shift))
            events.append({"type": "C", "frame": idx, "at": end})
            return end

        events: list[dict] = []
        cursor = 0
        for pid, roots in self.root_frames.items():
            if tenant_id is not None:
                roots = [root for root in roots if root.tenant_id == tenant_id]
            if not roots:
                continue
            tenant_ids = sorted({root.tenant_id for root in roots})
            pid_idx = index_of(("pid", pid), {"name": f"pid {pid}", "file": f"tenant {', '.join(map(str, tenant_ids))}"})
            shift = cursor - roots[0].time
            events.append({"type": "O", "frame": pid_idx, "at": cursor})
            for root in roots:
                cursor = emit(root, events, shift)
            events.append({"type": "C", "frame": pid_idx, "at": cursor})

        profiles = [{
            "type": "evented",
            "name": "all workers" if tenant_id is None else f"tenant {tenant_id}",
            "unit": "milliseconds",
            "startValue": 0,
            "endValue": cursor,
            "events": events,
        }] if events else []

        return {
            "$schema": SPEEDSCOPE_SCHEMA,
            "name": name,
            "activeProfileIndex": 0,
            "exporter": "pipeflame",
            "shared": {"frames": frames},
            "profiles": profiles,
        }
