"""Visit counts plus replay. No planner.

The driver picks the least-tried action that the identifiers it holds make constructible, runs it,
and records what changed. It never guesses a cause: a change on an entity with nothing in flight
against it is recorded as unattributed, because the unattributed rate is the test of whether the
cohort boundary is real.
"""

from __future__ import annotations

import random
import string
from collections import Counter, defaultdict

from explorer import projector


def run_id():
    """Six alphanumeric characters, because batchPaymentReference takes at most 16 and no hyphen."""
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))


class Trial:
    def __init__(self, action, key, status, changes, attributed):
        self.action = action
        self.key = key
        self.status = status
        self.changes = changes
        self.attributed = attributed

    def __repr__(self):
        mark = "" if self.attributed else " UNATTRIBUTED"
        return "<{} on {} -> {} {}{}>".format(
            self.action, self.key, self.status, sorted(self.changes), mark)


class Explorer:
    def __init__(self, actions, creates=None, budgeted=None):
        """`actions` maps a name to a callable taking the held identifiers and returning a Call."""
        self.actions = actions
        self.creates = creates or set()
        # Actions the driver runs to a budget rather than by least-tried. They are left out of
        # the frontier because they can never all be tried: a state whose only untried work is a
        # fault stays on the frontier for ever and the planner keeps routing to it. One run had
        # fault actions as the untried work at fourteen of nineteen frontier states.
        self.budgeted = budgeted or set()
        # How often an action was refused from a state, and whether it ever succeeded there.
        self.refused = defaultdict(int)
        self.refused_at = {}
        self.succeeded = set()
        self.steps = 0
        self.tried = defaultdict(int)
        self.visits = defaultdict(int)
        self.solo_trials = 0
        self.rejected = 0
        self.unattributed = 0
        # What was actually buildable while the run stood in each state. Without this the frontier
        # counts actions whose identifiers that state cannot supply, so it never clears.
        self.offered = defaultdict(set)
        self.races = 0
        self.race_findings = 0
        # Counted apart from visits: a race must never raise its own budget.
        self.solo = defaultdict(int)
        # The transition relation, learned rather than declared: which state each action landed
        # in, from the state it was taken in. This is what lets the driver route back to a state
        # with untried work instead of waiting to drift into it.
        self.edges = defaultdict(Counter)

    def constructible(self, held):
        """Actions every identifier of which the run already holds. A weaker claim than will succeed."""
        return [name for name, action in self.actions.items() if action.can_build(held)]

    REFUSALS_BEFORE_DROPPING = 2
    REFUSAL_DECAY_STEPS = 80

    def ruled_out(self, key, action):
        """True once an action has been refused from a state and never once worked there.

        A refusal is a precondition the run has discovered, so repeating the call spends a share
        of every round on something that cannot succeed. The rule is not permanent though: one
        refusal falls off every REFUSAL_DECAY_STEPS steps, so the run tries again now and then and
        notices if the service changes its answer.
        """
        # A success used to exempt the pair for good, so OpenAccount kept being chosen at
        # `account absent` and was refused 164 times with "An underfunded account already exists"
        # after a handful of early acceptances. A success now only clears the refusals recorded
        # before it, which is what makes the rule reflect the state the run is in now.
        count = self.refused[(key, action)]
        if count == 0:
            return False
        aged = (self.steps - self.refused_at.get((key, action), 0)) // self.REFUSAL_DECAY_STEPS
        live = count - aged
        if live <= 0:
            self.refused.pop((key, action), None)
            self.refused_at.pop((key, action), None)
            return False
        return live >= self.REFUSALS_BEFORE_DROPPING

    def offer(self, key, names):
        # An action that creates a new entity cannot change the state it is offered from, so it
        # would sit in that state's untried set for ever and hold the state on the frontier.
        self.offered[key].update(
            n for n in names
            if n not in self.creates and n not in self.budgeted and not self.ruled_out(key, n))

    def record(self, action, key, call, before, after, in_flight, landed=None):
        changes = projector.diff(before, after)
        # An action that creates a new entity reads back the NEW entity, so an edge from the
        # acting state to that reading is fabricated: CreateCustomer does not move a CLOSED
        # customer to PENDING, it makes a different customer that is PENDING. The planner walked
        # that false edge and re-planned it for ever.
        if landed is not None and action not in self.creates:
            self.edges[(key, action)][landed] += 1
        attributed = in_flight <= 1
        if changes and not attributed:
            self.unattributed += 1
        self.tried[(key, action)] += 1
        if call.ok:
            self.succeeded.add((key, action))
            self.refused.pop((key, action), None)
            self.refused_at.pop((key, action), None)
        else:
            self.refused[(key, action)] += 1
            self.refused_at[(key, action)] = self.steps
        self.visits[key] += 1
        self.solo[key] += 1
        trial = Trial(action, key, call.status, changes, attributed)
        self.solo_trials += 1
        if not 200 <= call.status < 300:
            self.rejected += 1
        return trial

    def frontier(self):
        """States with genuinely reachable untried actions, least visited first.

        An action counts only if the run has actually been able to build it from this state. An
        account action listed against a state with no account can never be tried, so counting it
        leaves an entry that never clears and hides the states worth returning to.
        """
        pending = []
        for key, seen in self.visits.items():
            untried = sorted(n for n in self.offered[key] if self.tried[(key, n)] == 0)
            if untried:
                pending.append((seen, key, untried))
        return sorted(pending)

    def record_race(self, key, finding):
        self.races += 1
        self.visits[key] += 1
        # A raced change belongs to the race as a whole. The unattributed count is for a change
        # with nothing in flight, which is what would show one cohort reaching into another.
        if finding:
            self.race_findings += 1

    def transitions(self):
        """The learned graph, as rows: which action took the run from one state to another."""
        rows = []
        for (from_key, action), landings in self.edges.items():
            for landed, count in landings.items():
                rows.append({
                    "from": list(from_key),
                    "action": action,
                    "to": list(landed),
                    "count": count,
                    "moved": from_key != landed,
                })
        return sorted(rows, key=lambda r: -r["count"])

    def route(self, start, target, limit=6):
        """The shortest learned action sequence from one state to another, or None.

        Only edges the run has actually walked count, so a route is a claim about what happened
        before, never a guess about what should work.
        """
        if start == target:
            return []
        seen, queue = {start}, [(start, [])]
        while queue:
            state, path = queue.pop(0)
            if len(path) >= limit:
                continue
            for (from_key, action), landings in self.edges.items():
                if from_key != state:
                    continue
                for landed in landings:
                    if landed in seen:
                        continue
                    if landed == target:
                        return path + [action]
                    seen.add(landed)
                    queue.append((landed, path + [action]))
        return None

    def summary(self):
        return {
            "trials": self.solo_trials + self.races,
            "races": self.races,
            "race findings": self.race_findings,
            "distinct states": len(self.visits),
            "rejections": self.rejected,
            "unattributed changes": self.unattributed,
            "frontier": len(self.frontier()),
        }
