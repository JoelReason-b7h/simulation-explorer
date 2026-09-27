"""A bank and its platforms that outlive the cycle, a clock that walks them through the calendar,
and a customer population that lives on them for simulated years.

Each fleet cycle used to stand up a new bank, so no bank aged past a quarter of an hour of
business days and no account had more than a few weeks of history. With SIM_LONG_LIVED_BANK on
(the default) fleet_cycle.py saves the cohort it stands up to SIM_SAVED_COHORT and, on every later
cycle, checks the saved cohort through the API and reuses it, standing up afresh only when it is
unusable. The runs of a reusing cycle see SIM_LONG_LIVED=1.

Three things run inside explore.py in that mode:

- Clock (the conductor, or a run with SIM_CLOCK_KEEPER=1): keeps the bank's business date on a
  line of SIM_DAYS_PER_HOUR simulated days per wall hour from the first tick, and for each day it
  advances runs that day's jobs in the scheduler's order: ACCRUALS_AND_REALISATIONS (which accrues
  the day and moves the date on by one calendar day, weekends and holidays included, because a
  Direct bank keeps its books every day), then DIRECT_DATA_FEED, DIRECT_DATA_RECON,
  DIRECT_MI_REPORT_DAILY, DIRECT_MI_REPORT_MONTHLY on the first of a month, TERM_PRODUCT_DISTRIBUTIONS,
  the notice processor and the closure sweep. The date only ever moves forward: the explorer's own
  AdvanceBusinessDay still runs, so the clock is a floor, not a ceiling.
- Population (every run, for its own platform): customers join, fund, top up, withdraw, move
  money between products, open TERMs that mature into a NOTICE account, change their nominated
  account, go quiet, and leave, each on a cadence counted in simulated days. The population is
  saved per platform, so customers live across cycles.
- Long-horizon checks: money conservation per customer over time, and nothing stuck: an
  instruction PENDING, an account CLOSING, or a clearing due EXPECTED for longer than allowed.

The notice due date, a TERM's maturity date and the top-up window are all counted on the wall
clock (NoticeProductService.getNoticeMaturityDate, MaturityDistributionService, TopUpFtdActivation
Service), not on the bank's business date, so compressing business time does not bring them
nearer. A bank that lives across cycles reaches them in real time instead: a two-day notice falls
due two wall days after it was placed, and a one-month TERM matures a month after it was funded.
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from explorer import fleet, interest_oracle, world

HERE = Path(__file__).resolve().parents[1]
ENABLED = os.environ.get("SIM_LONG_LIVED") == "1"
DAYS_PER_HOUR = float(os.environ.get("SIM_DAYS_PER_HOUR", "182.5"))
MAX_DAYS_PER_TICK = int(os.environ.get("SIM_MAX_DAYS_PER_TICK", "5"))
POPULATION_SECONDS = float(os.environ.get("SIM_POPULATION_SECONDS", "20"))
POPULATION_TARGET = int(os.environ.get("SIM_POPULATION_TARGET", "40"))
ACTIONS_PER_TICK = int(os.environ.get("SIM_POPULATION_ACTIONS", "3"))
STUCK_SECONDS = float(os.environ.get("SIM_STUCK_SECONDS", "300"))
STUCK_DAYS = int(os.environ.get("SIM_STUCK_DAYS", "30"))
STUCK_WALL_MINUTES = float(os.environ.get("SIM_STUCK_WALL_MINUTES", "60"))
STUCK_DUE_HOURS = float(os.environ.get("SIM_STUCK_DUE_HOURS", "24"))
ZERO = Decimal("0")


def saved_cohort_path():
    return Path(os.environ.get("SIM_SAVED_COHORT") or HERE / "long-lived.cohort.json")


def reuse_enabled():
    return os.environ.get("SIM_LONG_LIVED_BANK", "1") != "0"


def _write(path, data):
    path = Path(path)
    scratch = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    scratch.write_text(json.dumps(data, indent=1, sort_keys=True, default=str))
    os.replace(scratch, path)


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# -- the saved cohort ------------------------------------------------------------------------


def save_cohort(cohorts, ibans, cycle_name):
    path = saved_cohort_path()
    saved = _read(path) or {}
    if saved.get("cohorts") != cohorts:
        saved = {"cohorts": cohorts, "ibans": ibans, "createdAt": time.time(),
                 "createdBy": cycle_name, "cycles": []}
    saved["ibans"] = dict(saved.get("ibans") or {}, **ibans)
    saved["cycles"] = (saved.get("cycles") or []) + [cycle_name]
    _write(path, saved)
    return saved


def unusable(saved, count, settings):
    """Why the saved cohort cannot serve this cycle, or None when every check through the API
    and the read-only core lookups passes."""
    from explorer.client import BearerClient, DirectClient
    if not saved or not saved.get("cohorts"):
        return "no saved cohort"
    cohorts = saved["cohorts"]
    if len(cohorts) < count:
        return "the saved cohort holds {} platforms and the cycle wants {}".format(
            len(cohorts), count)
    if len({c.get("bankUid") for c in cohorts}) != 1:
        return "the saved platforms are not on one bank"
    bank = cohorts[0]["bankUid"]
    if interest_oracle.business_date(bank) is None:
        return "core holds no business date for bank {}".format(bank)
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    try:
        for cohort in cohorts[:count]:
            platform = cohort["platformUid"]
            account = ops.call("GET", "/operations/entity-internal-account/platforms/{}/currency/GBP"
                               .format(platform))
            if not account.ok:
                return "platform {} has no internal account ({})".format(platform, account.status)
            direct = DirectClient(settings["base_url"], settings["auth_token_url"],
                                  cohort["clientId"], "local", settings.get("auth_scope"))
            try:
                products = direct.call("GET", "/direct/v1/products")
            finally:
                direct.close()
            kinds = {row.get("productType") for row in (
                products.body.get("content") or [] if products.ok and isinstance(
                    products.body, dict) else [])}
            if not {"INSTANT", "NOTICE", "TERM"} <= kinds:
                return "platform {} lists products {} ({})".format(platform, sorted(kinds),
                                                                   products.status)
            iban = (saved.get("ibans") or {}).get(platform)
            rows, _ = interest_oracle._psql(
                "SELECT account_identifier->>'value' FROM entity_internal_account "
                "WHERE entity_uid = '{}' AND currency = 'GBP' AND account_identifier IS NOT NULL"
                .format(platform), timeout=60)
            if not iban or not rows or rows[0][0] != iban:
                return "platform {} virtual account {} does not match core's {}".format(
                    platform, iban, rows[0][0] if rows else None)
    finally:
        ops.close()
    return None


# -- the clock -------------------------------------------------------------------------------


def keeps_clock(run):
    return ENABLED and bool(getattr(run, "bank_uid", None)) and (
        not fleet.is_member() or os.environ.get("SIM_CLOCK_KEEPER") == "1")


_CLOCKS = {}


def clock_for(run):
    if id(run) not in _CLOCKS:
        _CLOCKS[id(run)] = Clock(run)
    return _CLOCKS[id(run)]


BANK_DAILY = ("DIRECT_DATA_FEED", "DIRECT_DATA_RECON", "DIRECT_MI_REPORT_DAILY")
BANK_TASK = "/operations/batch/processor/bank/{}/{}/sync"


class Clock:
    def __init__(self, run):
        self.run = run
        self.path = saved_cohort_path().with_name(saved_cohort_path().stem + ".clock.json")
        self.state = _read(self.path) or {}
        if self.state.get("bankUid") != run.bank_uid:
            self.state = {"bankUid": run.bank_uid}
        self.started_at = time.time()
        self.started_date = None
        self.days = []
        self.failures = {}

    def target(self, now):
        anchor_at, anchor_date = self.state.get("anchorAt"), self.state.get("anchorDate")
        if anchor_at is None or anchor_date is None:
            return None
        return date.fromisoformat(anchor_date) + timedelta(
            days=int((now - anchor_at) * DAYS_PER_HOUR / 3600))

    def tick(self, force=False):
        """Advance the bank up to the line, at most MAX_DAYS_PER_TICK days; answer days moved."""
        run = self.run
        now = time.time()
        today = interest_oracle.business_date(run.bank_uid)
        if today is None:
            return 0
        if self.started_date is None:
            self.started_date = today
        if self.state.get("anchorAt") is None:
            self.state.update(anchorAt=now, anchorDate=today.isoformat())
            _write(self.path, self.state)
        target = self.target(now)
        wanted = max(1 if force else 0, min(MAX_DAYS_PER_TICK, (target - today).days))
        moved = 0
        for _ in range(wanted):
            if not self.one_day():
                break
            moved += 1
        return moved

    def one_day(self):
        run = self.run
        before = interest_oracle.business_date(run.bank_uid)
        accrued = world.advance_business_day(run.ops, run.bank_uid)
        record = {"from": str(before), "at": round(time.time(), 1),
                  "ACCRUALS_AND_REALISATIONS": getattr(accrued, "status", None)}
        if not getattr(accrued, "ok", False):
            self._failed("ACCRUALS_AND_REALISATIONS", accrued)
            self.days.append(record)
            return False
        run.days_advanced += 1
        run.note_in_flight("interest", "the clock's day {}".format(before))
        new_date = interest_oracle.business_date(run.bank_uid)
        tasks = list(BANK_DAILY)
        if new_date and new_date.day == 1:
            tasks.append("DIRECT_MI_REPORT_MONTHLY")
        tasks.append("TERM_PRODUCT_DISTRIBUTIONS")
        for task in tasks:
            call = run.ops.call("POST", BANK_TASK.format(run.bank_uid, task))
            record[task] = call.status
            if not call.ok:
                self._failed(task, call)
        run.note_in_flight("feed", "the clock's feed and RECON for {}".format(new_date))
        for label, call in (("notice", world.process_due_notice(run.ops)),
                            ("closures", world.process_closures(run.ops))):
            record[label] = call.status
            if not call.ok:
                self._failed(label, call)
        record["to"] = str(new_date)
        self.days.append(record)
        del self.days[:-60]
        self.state["ticks"] = self.state.get("ticks", 0) + 1
        self.state["lastDate"] = str(new_date)
        _write(self.path, self.state)
        return True

    def _failed(self, task, call):
        entry = self.failures.setdefault(task, {"count": 0})
        entry["count"] += 1
        entry["last"] = "{} {}".format(getattr(call, "status", None),
                                       str(getattr(call, "body", ""))[:200])

    def snapshot(self):
        today = interest_oracle.business_date(self.run.bank_uid)
        minutes = max((time.time() - self.started_at) / 60.0, 1e-6)
        moved = (today - self.started_date).days if today and self.started_date else 0
        return {"businessDate": str(today), "startedAt": str(self.started_date),
                "daysMovedThisRun": moved, "daysPerWallMinute": round(moved / minutes, 2),
                "clockDaysThisRun": len([d for d in self.days if d.get("to")]),
                "target": str(self.target(time.time())), "daysPerHour": DAYS_PER_HOUR,
                "failures": self.failures, "recentDays": self.days[-5:]}


# -- the population --------------------------------------------------------------------------

PERSONAS = ("saver", "spender", "mover", "termer", "quiet", "leaver", "changer", "saver")
CADENCE = {"saver": 30, "spender": 20, "mover": 45, "termer": 30, "quiet": 10 ** 6,
           "leaver": 120, "changer": 90}


class Population:
    def __init__(self, run):
        from explorer.journeys import Journey
        self.run = run
        self.Journey = Journey
        self.path = saved_cohort_path().with_name("{}.population.{}.json".format(
            saved_cohort_path().stem, run.platform_uid))
        self.state = _read(self.path) or {"customers": {}, "lastDay": None, "oracleSid": 0,
                                          "seen": {}}
        self.ticked_at = 0.0
        self.stuck_at = 0.0
        self.random = random.Random(run.platform_uid)
        self.actions = []
        self.joined = 0

    def save(self):
        _write(self.path, self.state)

    def live(self):
        return {c: s for c, s in self.state["customers"].items() if not s.get("left")}

    def journey(self, customer):
        j = self.Journey(self.run, "LongLife")
        j.customer = customer
        return j

    def tick(self):
        now = time.time()
        if now - self.ticked_at < POPULATION_SECONDS:
            return
        self.ticked_at = now
        today = interest_oracle.business_date(self.run.bank_uid)
        if today is None:
            return
        last = date.fromisoformat(self.state["lastDay"]) if self.state.get("lastDay") else None
        self.state["lastDay"] = today.isoformat()
        budget = ACTIONS_PER_TICK
        if last is None or (today - last).days > 0 or not self.live():
            if len(self.live()) < POPULATION_TARGET:
                self.join(today)
                budget -= 1
        due = sorted((s.get("next") or "", c) for c, s in self.live().items()
                     if s.get("next") and s["next"] <= today.isoformat())
        for _, customer in due[:max(0, budget)]:
            self.act(customer, today)
        self.conserve_one()
        if now - self.stuck_at >= STUCK_SECONDS:
            self.stuck_at = now
            self.nothing_stuck(today)
        self.save()

    def record(self, customer, what, outcome):
        self.actions.append({"customer": customer, "what": what, "outcome": str(outcome)[:160],
                             "trial": self.run.steps})
        del self.actions[:-40]

    def join(self, today):
        run = self.run
        persona = PERSONAS[len(self.state["customers"]) % len(PERSONAS)]
        j = self.journey(None)
        if not j.new_customer():
            self.record(j.customer, "join as " + persona, "not ACTIVATED")
            return
        instant = j.product("INSTANT")
        account = j.open(instant, "INSTANT") if instant else None
        entry = {"persona": persona, "joined": today.isoformat(), "accounts": {}, "funded": "0",
                 "next": (today + timedelta(days=CADENCE[persona])).isoformat()}
        if account:
            entry["accounts"]["INSTANT"] = account
            j.fund([(account, Decimal("20.00"))])
            if getattr(j, "batch_accepted", False):
                entry["funded"] = "20.00"
        if persona == "termer":
            term, notice = j.product("TERM 1 month"), j.product("NOTICE")
            a_term = j.open(term, "TERM", Decimal("50.00")) if term else None
            a_notice = j.open(notice, "NOTICE") if notice else None
            if a_term:
                entry["accounts"]["TERM"] = a_term
                j.fund([(a_term, Decimal("50.00"))])
                if getattr(j, "batch_accepted", False):
                    entry["funded"] = str(Decimal(entry["funded"]) + Decimal("50.00"))
            if a_notice:
                entry["accounts"]["NOTICE"] = a_notice
            entry["destinationWanted"] = bool(a_term and a_notice)
        self.state["customers"][j.customer] = entry
        self.joined += 1
        self.record(j.customer, "join as " + persona, "accounts {}".format(
            sorted(entry["accounts"])))

    def act(self, customer, today):
        entry = self.state["customers"][customer]
        persona = entry["persona"]
        j = self.journey(customer)
        instant = entry["accounts"].get("INSTANT")
        entry["next"] = (today + timedelta(days=CADENCE[persona])).isoformat()
        if entry.get("destinationWanted") and entry["accounts"].get("TERM"):
            term = j.account(entry["accounts"]["TERM"]["accountId"])
            if term.get("status") == "OPEN" and not term.get("maturityDestination"):
                call = j.client.call("POST", "/direct/v1/customers/{}/accounts/{}/"
                                     "maturityDestination".format(
                                         customer, entry["accounts"]["TERM"]["accountId"]),
                                     json_body={"productId": entry["accounts"]["NOTICE"][
                                         "productId"]})
                entry["destinationWanted"] = not call.ok
                self.record(customer, "set the TERM's maturity destination", call.status)
                return
            entry["termStatus"] = term.get("status")
        if not instant:
            return
        if persona == "saver":
            call = j.fund([(instant, Decimal("5.00"))], customer=customer)
            if getattr(j, "batch_accepted", False):
                entry["funded"] = str(Decimal(entry["funded"]) + Decimal("5.00"))
            self.record(customer, "top up 5.00", call.status)
        elif persona == "spender":
            balance = Decimal(str(j.account(instant["accountId"]).get("balance") or "0"))
            if balance > 2:
                call = j.instruct(instant, "WITHDRAW", Decimal("1.00"))
                self.record(customer, "withdraw 1.00", call.status)
        elif persona == "mover":
            notice = entry["accounts"].get("NOTICE")
            destination = notice["productId"] if notice else j.product("NOTICE")
            if destination and not j.pending(instant["accountId"]):
                call = j.instruct(instant, "TRANSFER", Decimal("2.00"), destination=destination)
                self.record(customer, "transfer 2.00 to NOTICE", call.status)
                if call.ok:
                    entry["moved"] = str(Decimal(entry.get("moved") or "0") + Decimal("2.00"))
                if call.ok and not notice:
                    accounts = j.accounts(customer) or {}
                    for account_id, account in accounts.items():
                        if (account.get("product") or {}).get("productId") == destination:
                            entry["accounts"]["NOTICE"] = {
                                "accountId": account_id, "productId": destination,
                                "accountReference": account.get("accountReference"),
                                "productType": "NOTICE"}
        elif persona == "changer" and not j.pending():
            # Only with nothing in flight: a change while a withdrawal is pending is FINDINGS.md 24.
            from explorer.journeys import PAYEES, _nominate
            pair = PAYEES["A"] if entry.get("payee") != "A" else PAYEES["C"]
            call = _nominate(j, "Long life payee {}".format(customer[:6]), pair)
            entry["payee"] = "A" if pair == PAYEES["A"] else "C"
            self.record(customer, "change the nominated account", call.status)
        elif persona == "leaver":
            self.leave(customer, entry, j, instant)

    def leave(self, customer, entry, j, instant):
        """Close the INSTANT account once nothing is in flight, then the customer once every
        account is empty. Never while an instruction is pending (FINDINGS.md 15)."""
        if j.pending():
            self.record(customer, "leave", "an instruction is in flight, so not yet")
            return
        read = j.account(instant["accountId"])
        if read.get("status") == "OPEN":
            call = j.client.call("POST", "/direct/v1/customers/{}/accounts/{}/close?reason="
                                 "NO_LONGER_NEEDED".format(customer, instant["accountId"]))
            self.record(customer, "close the INSTANT account", call.status)
            return
        if read.get("status") == "CLOSED":
            accounts = j.accounts(customer) or {}
            if all(Decimal(str(a.get("balance") or "0")) == 0 for a in accounts.values()):
                call = j.client.call("POST", "/direct/v1/customers/{}/close".format(customer))
                self.record(customer, "close the customer", call.status)
                if call.ok:
                    entry["left"] = True

    def conserve_one(self):
        """Hold one customer's whole history against what the population put in and took out."""
        live = sorted(self.live())
        if not live:
            return
        index = self.state.get("conserveIndex", 0) % len(live)
        self.state["conserveIndex"] = index + 1
        customer = live[index]
        entry = self.state["customers"][customer]
        j = self.journey(customer)
        if j.pending():
            return
        read = j.money()
        if read is None:
            return
        total, by_type = read
        funded = Decimal(entry.get("funded") or "0")
        moved = Decimal(entry.get("moved") or "0")
        deposits = by_type.get("DEPOSIT", ZERO) - moved
        transfers = by_type.get("TRANSFER", ZERO)
        booked = sum(by_type.values(), ZERO)
        run = self.run
        completed = sum((Decimal(str(r.get("amount") or "0")) for r in j.instructions() or []
                         if r.get("type") == "DEPOSIT" and r.get("status") == "COMPLETED"),
                        ZERO)
        # A deposit the service refused is funded and never booked, so the rule is broken only
        # when the booked deposits disagree with the completed deposit instructions too, or when
        # more completed than was funded.
        if deposits.compare(funded) != 0 and (deposits.compare(completed) != 0
                                               or completed > funded):
            run.note_violation("LongLife", "over its life a customer's deposits are what it funded",
                               customer, "{} customer joined {} has DEPOSIT rows summing to {} "
                               "against {} funded".format(entry["persona"], entry["joined"],
                                                          deposits, funded), funded, deposits)
        if transfers.compare(ZERO) != 0:
            run.note_violation("LongLife", "over its life a customer's transfers cancel",
                               customer, "TRANSFER rows sum to {}".format(transfers), "0",
                               transfers)
        if total.compare(booked) != 0:
            # The balances and the transactions are two reads, and the clock keeper realises a day
            # of interest every few seconds: box fleets 20 and 21 each saw 0.01 between them. Only
            # a gap that survives a second read is reported.
            again = j.money()
            if again is not None:
                total, by_type = again
                booked = sum(by_type.values(), ZERO)
        if total.compare(booked) != 0:
            run.note_violation("LongLife", "over its life a customer's balances are its "
                               "transactions", customer, "the accounts hold {} and every "
                               "transaction sums to {}".format(total, booked), booked, total,
                               body={k: str(v) for k, v in by_type.items()})

    def nothing_stuck(self, today):
        """An instruction PENDING or an account CLOSING for more than STUCK_DAYS simulated days
        and STUCK_WALL_MINUTES wall minutes, or a clearing due EXPECTED past STUCK_DUE_HOURS."""
        run = self.run
        seen = self.state.setdefault("seen", {})
        now = time.time()
        current = set()
        for customer, entry in self.live().items():
            j = self.journey(customer)
            notice_accounts = {a["accountId"] for k, a in entry["accounts"].items()
                               if k == "NOTICE"}
            for row in j.pending() or []:
                if row.get("accountId") in notice_accounts and row.get("type") == "WITHDRAWAL":
                    # Counted on the wall clock from placement: two days' notice, then a day.
                    key, limit_minutes = "instruction " + str(row.get("instructionId")), 3 * 1440
                else:
                    key, limit_minutes = "instruction " + str(row.get("instructionId")), None
                current.add(key)
                self._judge(seen, key, today, now, customer, "an instruction does not stay "
                            "PENDING", "{} {} {}".format(row.get("type"), row.get("amount"),
                                                         row.get("instructionId")),
                            limit_minutes)
            for kind, account in entry["accounts"].items():
                status = j.account(account["accountId"]).get("status")
                if status == "CLOSING":
                    key = "closing " + account["accountId"]
                    current.add(key)
                    self._judge(seen, key, today, now, customer,
                                "an account does not stay CLOSING", "{} account {}".format(
                                    kind, account["accountId"]), None)
        for key in list(seen):
            if key not in current:
                seen.pop(key, None)
        customers = list(self.live())
        if customers:
            from explorer import ledger
            rows = ledger._psql(
                "SELECT ppd.uid, ppd.value_amount, ppd.payment_direction, ppd.due_date, ao.uid "
                "FROM partner_payment_due ppd JOIN internal_account ia ON ia.sid = ppd.account_sid "
                "JOIN account_owner ao ON ao.sid = ia.account_owner_sid "
                "WHERE ppd.payment_status = 'EXPECTED' AND ppd.due_date < now() - interval "
                "'{} hours' AND ao.uid IN ({}) LIMIT 20".format(
                    STUCK_DUE_HOURS, ",".join("'{}'".format(c) for c in customers)))
            for due, amount, direction, due_date, owner in rows:
                run.note_violation("LongLife", "a payment due does not stay EXPECTED", owner,
                                   "{} due {} of {} was due {} and is still EXPECTED".format(
                                       direction, due, amount, due_date),
                                   "settled within {}h".format(STUCK_DUE_HOURS), "EXPECTED")

    def _judge(self, seen, key, today, now, customer, rule, what, limit_minutes):
        first = seen.setdefault(key, {"day": today.isoformat(), "at": now})
        days = (today - date.fromisoformat(first["day"])).days
        minutes = (now - first["at"]) / 60.0
        if limit_minutes is not None:
            late = minutes > limit_minutes
        else:
            late = days > STUCK_DAYS and minutes > STUCK_WALL_MINUTES
        if late and not first.get("reported"):
            first["reported"] = True
            self.run.note_violation("LongLife", rule, customer,
                                    "{} first seen on business date {} ({:.0f} wall minutes and "
                                    "{} simulated days ago)".format(what, first["day"], minutes,
                                                                    days),
                                    "moved on within {} days".format(STUCK_DAYS),
                                    "{} days".format(days))

    def snapshot(self):
        live = self.live()
        personas = {}
        for entry in live.values():
            personas[entry["persona"]] = personas.get(entry["persona"], 0) + 1
        return {"live": len(live), "left": len(self.state["customers"]) - len(live),
                "joinedThisRun": self.joined, "personas": personas,
                "lastDay": self.state.get("lastDay"), "recent": self.actions[-10:]}


class LongLife:
    """What explore.py holds in long-lived mode: the clock (if this run keeps it) and the
    population of this run's platform."""

    def __init__(self, run):
        self.run = run
        self.clock = clock_for(run) if keeps_clock(run) else None
        self.population = Population(run) if run.platform_uid and run.bank_uid else None

    def tick(self):
        if self.clock:
            try:
                self.clock.tick()
            except Exception as fault:  # noqa: BLE001 - the clock must not end the run
                print("  -- the clock raised {}: {}".format(type(fault).__name__, fault))
        if self.population:
            try:
                self.population.tick()
            except Exception as fault:  # noqa: BLE001 - nor must the population
                print("  -- the population raised {}: {}".format(type(fault).__name__, fault))

    def oracle_since(self):
        return int((self.population.state if self.population else {}).get("oracleSid") or 0)

    def remember_oracle(self, newest):
        if self.population:
            self.population.state["oracleSid"] = int(newest)
            self.population.save()

    def snapshot(self):
        return {"clock": self.clock.snapshot() if self.clock else None,
                "population": self.population.snapshot() if self.population else None}
