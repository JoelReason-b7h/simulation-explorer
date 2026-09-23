"""Fires several actions at one entity at the same instant, and judges the outcome as a set.

A raced step deliberately breaks the rule §5 relies on — that a diff is attributable only while one
action is in flight against an entity — so the harness must not ask which action caused what. It
asks a different question instead: of a set of actions that cannot all legitimately win, how many
came back accepted, and what state did the entity end in.

Races belong on one entity. Two actions against different entities are concurrent, not racing.
"""

from __future__ import annotations

import itertools
import time
from concurrent.futures import ThreadPoolExecutor

# Nothing here lists which actions to race. A combination is composed from what the run can build
# at this state, and how many of its calls may legitimately win is derived from what the actions
# do, so a new action joins the races the moment it joins the catalogue.


def _changes(name, catalogue):
    """True when the call changes the entity rather than reading it."""
    action = catalogue.get(name)
    return getattr(action, "method", "GET").upper() != "GET"


def _entity(name, catalogue):
    action = catalogue.get(name)
    return getattr(action, "entity", None) or "account"


def limits(names, catalogue, spends, world=(), driven=()):
    """How many of a combination's calls may legitimately be accepted, per entity.

    Judged per entity because a combination reaches across entities: closing an account and adding
    a nominated account to its customer are concurrent, not racing, so one limit over the whole
    combination reported a violation every time an unrelated call happened to be in the same set.

    Counting winners proves very little here, so this limits almost nothing:

    - A spend racing a call that does not spend has an ordering where the other call lands first
      and the spend lands second, which leaves both correctly accepted.
    - Two spends racing each other both answer 200, because CloseCustomer is idempotent: closing a
      customer that is already CLOSED changes nothing and is not an error.
    - A driven action carries no body in the catalogue, yet it builds a fresh reference each time
      it runs, so two of them racing make two different objects and both are right to be accepted.

    What a race can prove is in RaceOutcome.verdict: a call that failed rather than refused, and a
    spend that was accepted and moved nothing.
    """
    grouped = {}
    for name in names:
        # A world action moves every cohort's money or every bank's date, so it conflicts with
        # nothing and belongs to no entity's limit.
        if name in world or not _changes(name, catalogue):
            continue
        grouped.setdefault(_entity(name, catalogue), []).append(name)

    found = {}
    for entity, group in grouped.items():
        repeated_bodyless = (
            len(set(group)) == 1 and len(group) > 1
            and group[0] not in spends
            and group[0] not in driven
            and getattr(catalogue.get(group[0]), "body", None) is None)
        # The same action twice with no body carries the same reference both times, so the second
        # is a repeat of the first. With a body each occurrence mints a fresh reference, which
        # makes two different objects and two correct acceptances.
        found[entity] = 1 if repeated_bodyless else len(group)
    return found


def _message(call):
    if isinstance(call.body, dict):
        return call.body.get("message")
    return None


class RaceOutcome:
    """What a set of calls fired together came back with, judged as a set rather than one by one.

    A race cannot say which call caused what, so it asks what it can answer from evidence: did any
    call fail rather than refuse, and did a call that was accepted actually move anything.
    """

    def __init__(self, names, results, before, after, elapsed, catalogue=None, world=(),
                 spends=(), terminal=()):
        self.names = names
        self.results = results
        self.before = before
        self.after = after
        self.elapsed = elapsed
        self.spends = set(spends or ())
        self.terminal = terminal or {}
        self.statuses = [getattr(r, "status", 0) for r in results]
        self.catalogue = catalogue = catalogue or {}
        self.world = set(world or ())
        # A read succeeding is not winning anything, and neither is a world action, which acts on
        # the whole stack rather than on this entity. Counting either reported a violation for
        # every combination that happened to include one.
        self.accepted = [
            n for n, r in zip(names, results)
            if getattr(r, "ok", False)
            and n not in self.world
            and getattr(catalogue.get(n), "method", "GET").upper() != "GET"]

    def server_errors(self):
        """Every call that failed rather than refused, as (action, status, message)."""
        return [(n, getattr(r, "status", 0), _message(r))
                for n, r in zip(self.names, self.results)
                if (getattr(r, "status", 0) or 0) >= 500]

    def ended_wrong(self):
        """A spend that came back accepted and left the entity somewhere it can still be used.

        This is what a race can prove, unlike counting winners: the service said it ended the
        entity, and the read taken afterwards says the entity is not ended.
        """
        if not isinstance(self.after, dict) or not isinstance(self.before, dict):
            return None
        for name, result in zip(self.names, self.results):
            if name not in self.spends or not getattr(result, "ok", False):
                continue
            entity = _entity(name, self.catalogue)
            field = "customerStatus" if entity == "customer" else "status"
            was, now = self.before.get(field), self.after.get(field)
            if not was or was != now:
                continue
            # The status is compared rather than tested against a terminal set, because closing is
            # asynchronous on some paths and the entity legitimately sits at an in-between status
            # for a while. Standing completely still is what cannot be explained by a slow close.
            if was in self.terminal.get(entity, ()):
                # It was already ended before the race, so accepting the call again changed
                # nothing and was right to change nothing.
                continue
            return ("{} was accepted and the {} is still {}, unchanged from before the race, so "
                    "the call that ends it moved nothing".format(name, entity, was))
        return None

    def verdict(self, allowed, ignore_server_errors=False):
        """What the race shows, in the terms the run can act on.

        `allowed` maps each entity to how many of the calls against it may be accepted. More than
        that on any one entity is the finding: two winners where the domain permits one.

        `ignore_server_errors` is set while the run has a fault injected, because a cut wire makes
        every call fail and that says nothing about how the service handles concurrency.
        """
        errors = [] if ignore_server_errors else self.server_errors()
        if errors:
            return "server error under race: {}".format(
                "; ".join("{} {} {}".format(n, s, m) for n, s, m in errors))
        left_open = self.ended_wrong()
        if left_open:
            return left_open
        for entity, limit in sorted(allowed.items()):
            won = [n for n in self.accepted if _entity(n, self.catalogue) == entity]
            if len(won) > limit:
                return "{} of {} calls on the {} were accepted, at most {} should win: {}".format(
                    len(won), len(self.names), entity, limit, ", ".join(won))
        return None


def fire(calls):
    """Runs the calls together and returns them in order.

    The barrier matters: submitting to a pool is not the same as starting together, and a race the
    service wins on ordering alone proves nothing about its locking.
    """
    started = time.time()
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(fn) for fn in calls]
        results = [f.result() for f in futures]
    return results, (time.time() - started) * 1000


def candidates(constructible, catalogue=None, spends=None, width=3, world=(), driven=()):
    """Every combination the run can build right now, with how many of its calls may win.

    Composed rather than listed: each action raced against itself, then pairs and triples drawn
    from what is constructible here. The driver picks between them by how often each has been
    tried at this state, the same way it picks a single action, so combinations that have never
    run come first and the space gets covered without anybody choosing for it.
    """
    catalogue = catalogue or {}
    spends = spends or set()
    names = sorted(constructible)
    found = []

    for name in names:
        found.append(((name, name), limits((name, name), catalogue, spends, world, driven)))

    for size in (2, 3):
        if size > width:
            break
        for combination in itertools.combinations(names, size):
            # A triple only earns its cost when at least one call spends the entity, because three
            # reads racing each other shows nothing the run does not already know.
            if size == 3 and not any(n in spends for n in combination):
                continue
            found.append((combination, limits(combination, catalogue, spends, world, driven)))

    return found
