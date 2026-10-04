import { test } from "node:test";
import assert from "node:assert/strict";
import { classifyTags, parseLocal, addInterval, nextDue } from "./lib.js";

test("classifyTags parses valid tags of every unit", () => {
  const { parsed, invalid } = classifyTags(["after-14d", "after-6w", "after-1m", "after-2y", "work"]);
  assert.deepEqual(parsed.map((p) => [p.n, p.unit]), [[14, "d"], [6, "w"], [1, "m"], [2, "y"]]);
  assert.deepEqual(invalid, []);
});

test("classifyTags accepts a leading # and any case", () => {
  assert.deepEqual(classifyTags(["#After-3D"]).parsed.map((p) => [p.n, p.unit]), [[3, "d"]]);
});

test("classifyTags flags unparseable after-* tags and ignores unrelated tags", () => {
  const bad = ["after-", "after-d", "after-0d", "after-3x", "after-1.5d", "after-1d2", "after-99999d", "after--1d"];
  const { parsed, invalid } = classifyTags([...bad, "afterwards", "after"]);
  assert.equal(parsed.length, 0);
  assert.deepEqual(invalid, bad);
});

test("classifyTags tolerates missing tags", () => {
  assert.deepEqual(classifyTags(undefined), { parsed: [], invalid: [] });
});

test("parseLocal reads date-only, timed and microsecond timestamps", () => {
  assert.deepEqual(parseLocal("2026-10-02T06:48:41.469695"), { y: 2026, mo: 10, d: 2, hh: 6, mm: 48 });
  assert.deepEqual(parseLocal("2026-10-01T00:00:00"), { y: 2026, mo: 10, d: 1, hh: 0, mm: 0 });
  assert.deepEqual(parseLocal("2026-10-01"), { y: 2026, mo: 10, d: 1, hh: 0, mm: 0 });
});

test("parseLocal rejects garbage and impossible dates", () => {
  for (const bad of [undefined, null, "", "tomorrow", "2026-02-30", "2026-13-01"]) assert.equal(parseLocal(bad), null);
});

test("days and weeks cross month and year boundaries", () => {
  assert.deepEqual(addInterval({ y: 2026, mo: 12, d: 30 }, 5, "d"), { y: 2027, mo: 1, d: 4 });
  assert.deepEqual(addInterval({ y: 2026, mo: 10, d: 2 }, 6, "w"), { y: 2026, mo: 11, d: 13 });
  assert.deepEqual(addInterval({ y: 2028, mo: 2, d: 28 }, 1, "d"), { y: 2028, mo: 2, d: 29 });
});

test("months clamp to the last valid day", () => {
  assert.deepEqual(addInterval({ y: 2026, mo: 1, d: 31 }, 1, "m"), { y: 2026, mo: 2, d: 28 });
  assert.deepEqual(addInterval({ y: 2028, mo: 1, d: 31 }, 1, "m"), { y: 2028, mo: 2, d: 29 }); // leap year
  assert.deepEqual(addInterval({ y: 2026, mo: 3, d: 31 }, 1, "m"), { y: 2026, mo: 4, d: 30 });
  assert.deepEqual(addInterval({ y: 2026, mo: 8, d: 31 }, 6, "m"), { y: 2027, mo: 2, d: 28 });
});

test("months roll over years, including multiples of 12", () => {
  assert.deepEqual(addInterval({ y: 2026, mo: 11, d: 15 }, 3, "m"), { y: 2027, mo: 2, d: 15 });
  assert.deepEqual(addInterval({ y: 2026, mo: 12, d: 31 }, 1, "m"), { y: 2027, mo: 1, d: 31 });
  assert.deepEqual(addInterval({ y: 2026, mo: 5, d: 10 }, 24, "m"), { y: 2028, mo: 5, d: 10 });
});

test("years clamp 29 Feb to 28 Feb off leap years and keep it on leap years", () => {
  assert.deepEqual(addInterval({ y: 2024, mo: 2, d: 29 }, 1, "y"), { y: 2025, mo: 2, d: 28 });
  assert.deepEqual(addInterval({ y: 2024, mo: 2, d: 29 }, 4, "y"), { y: 2028, mo: 2, d: 29 });
  assert.deepEqual(addInterval({ y: 2100, mo: 2, d: 28 }, 0 + 1, "y"), { y: 2101, mo: 2, d: 28 }); // 2100 not a leap year
});

test("addInterval rejects unknown units", () => {
  assert.throws(() => addInterval({ y: 2026, mo: 1, d: 1 }, 1, "x"));
});

test("nextDue keeps all-day reminders date-only", () => {
  const out = nextDue({ completion: parseLocal("2026-10-02T06:48:41"), dueTime: parseLocal("2026-10-01T00:00:00"), allDay: true, n: 1, unit: "m" });
  assert.equal(out, "2026-11-02");
});

test("nextDue preserves the original due time of day, not the completion time", () => {
  const out = nextDue({ completion: parseLocal("2026-10-02T23:59:00"), dueTime: parseLocal("2026-09-30T09:05:00"), allDay: false, n: 14, unit: "d" });
  assert.equal(out, "2026-10-16 09:05");
});

test("nextDue uses completion date (not old due date) and clamps month end", () => {
  const out = nextDue({ completion: parseLocal("2026-01-31T08:00:00"), dueTime: parseLocal("2025-12-01T17:30:00"), allDay: false, n: 1, unit: "m" });
  assert.equal(out, "2026-02-28 17:30");
});

test("nextDue is unaffected by DST changeover dates", () => {
  const out = nextDue({ completion: parseLocal("2026-03-28T10:00:00"), dueTime: parseLocal("2026-03-01T01:30:00"), allDay: false, n: 1, unit: "d" });
  assert.equal(out, "2026-03-29 01:30"); // UK clocks skip 01:00-02:00 that day; we emit wall-clock text only
});

import { planReminder, sameDue } from "./lib.js";

const NOW = new Date(2026, 9, 4, 13, 0);
const base = { id: 1, title: "t", completed: true, allDay: true, dueDate: "2026-10-01T00:00:00", completionDate: "2026-10-02T06:48:41.469695", tags: ["after-1m"] };

test("planReminder ignores untagged and open untagged-recurrence reminders", () => {
  assert.equal(planReminder({ ...base, tags: undefined }, NOW).action, "ignore");
  assert.equal(planReminder({ ...base, tags: ["work"] }, NOW).action, "ignore");
  assert.equal(planReminder({ ...base, completed: false }, NOW).action, "ignore");
});

test("planReminder reschedules a completed tagged reminder from its completion date", () => {
  assert.deepEqual(planReminder(base, NOW), { action: "reschedule", due: "2026-11-02", stripRecurrence: false, completionSource: "remctl", interval: "after-1m" });
});

test("planReminder keeps timed reminders timed", () => {
  const p = planReminder({ ...base, allDay: false, dueDate: "2026-10-01T09:00:00" }, NOW);
  assert.equal(p.due, "2026-11-02 09:00");
});

test("planReminder falls back to run time when completion date is unreadable", () => {
  const p = planReminder({ ...base, completionDate: undefined }, NOW);
  assert.equal(p.completionSource, "run-time-fallback");
  assert.equal(p.due, "2026-11-04");
});

test("planReminder strips native recurrence on completed and open tagged reminders", () => {
  const rec = { frequency: "monthly", interval: 1 };
  assert.equal(planReminder({ ...base, recurrence: rec }, NOW).stripRecurrence, true);
  assert.equal(planReminder({ ...base, completed: false, recurrence: rec }, NOW).action, "strip-recurrence");
});

test("planReminder skips unparseable, ambiguous and due-less reminders without scheduling", () => {
  assert.match(planReminder({ ...base, tags: ["after-3x"] }, NOW).reason, /unparseable/);
  assert.match(planReminder({ ...base, tags: ["after-1d", "after-2d"] }, NOW).reason, /multiple/);
  assert.match(planReminder({ ...base, tags: ["after-1d", "after-bad"] }, NOW).reason, /unparseable/);
  assert.match(planReminder({ ...base, dueDate: undefined }, NOW).reason, /due date/);
});

test("sameDue compares day for all-day and day+time for timed", () => {
  assert.equal(sameDue("2026-11-02T00:00:00", "2026-11-02", true), true);
  assert.equal(sameDue("2026-11-03T00:00:00", "2026-11-02", true), false);
  assert.equal(sameDue("2026-11-02T09:00:00", "2026-11-02 09:00", false), true);
  assert.equal(sameDue("2026-11-02T10:00:00", "2026-11-02 09:00", false), false);
});
