"""Which devices have been offline for too long.

matter-server logs an error for every retry while a device is unreachable, and a device that
drops off for a few minutes (a lossy Thread link, a sleepy device) comes back by itself. Those
errors are demoted to warnings (see matter-server-log); what's worth an error is a device that
stays offline. This keeps the offline-since time of each device; the bridge asks it which ones
are overdue and logs an error once per outage.

Nothing here knows about Matter or mqtt, and the clock is injectable, so it's testable alone.
"""
import time


class OfflineTracker:
    def __init__(self, threshold_secs, clock=time.monotonic):
        self.threshold_secs = threshold_secs
        self.clock = clock
        self._offline_since = {}  # node_id -> clock() when it went offline
        self._reported = set()    # node_ids already reported as overdue, this outage

    def update(self, node_id, online):
        """A device's availability, as published. Returns the seconds it was offline if it
        just came back after being reported as overdue, otherwise None."""
        if not online:
            self._offline_since.setdefault(node_id, self.clock())
            return None
        since = self._offline_since.pop(node_id, None)
        if node_id in self._reported:
            self._reported.discard(node_id)
            return self.clock() - since
        return None

    def retain(self, node_ids):
        """Forget the devices that aren't in node_ids any more (removed from the fabric)."""
        for node_id in set(self._offline_since) - set(node_ids):
            del self._offline_since[node_id]
            self._reported.discard(node_id)

    def overdue(self):
        """Devices offline for longer than the threshold and not reported yet, as
        [(node_id, seconds offline)]. Each is returned once per outage."""
        now = self.clock()
        out = []
        for node_id, since in self._offline_since.items():
            if node_id not in self._reported and now - since > self.threshold_secs:
                self._reported.add(node_id)
                out.append((node_id, now - since))
        return out
