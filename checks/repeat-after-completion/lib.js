// Pure logic for the repeat-after-completion check: tag parsing and date arithmetic.
// All dates are handled as naive local wall-clock components (RemCTL emits no timezone),
// and arithmetic runs through Date.UTC so DST shifts can never move a time of day.

const TAG_PREFIX = /^after-/i;
const TAG_FULL = /^after-(\d+)([dwmy])$/i;
const MAX_INTERVAL = 3650;
const LOCAL_TS = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/;

/** Returns the reminder's after-* tags, split into parsed intervals and unparseable ones. */
export function classifyTags(tags) {
  const parsed = [];
  const invalid = [];
  for (const raw of tags ?? []) {
    const tag = String(raw).replace(/^#/, "");
    if (!TAG_PREFIX.test(tag)) continue;
    const m = TAG_FULL.exec(tag);
    const n = m ? Number(m[1]) : 0;
    if (!m || n < 1 || n > MAX_INTERVAL) invalid.push(tag);
    else parsed.push({ tag, n, unit: m[2].toLowerCase() });
  }
  return { parsed, invalid };
}

/** Parses 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS[.ffffff]]' into components, or null. */
export function parseLocal(text) {
  const m = LOCAL_TS.exec(String(text ?? ""));
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const probe = new Date(Date.UTC(y, mo - 1, d));
  if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== mo - 1 || probe.getUTCDate() !== d) return null;
  return { y, mo, d, hh: Number(m[4] ?? 0), mm: Number(m[5] ?? 0) };
}

export function daysInMonth(y, mo) {
  return new Date(Date.UTC(y, mo, 0)).getUTCDate();
}

/** Adds an interval to a date, clamping month/year results to the last valid day. */
export function addInterval({ y, mo, d }, n, unit) {
  if (unit === "d" || unit === "w") {
    const t = new Date(Date.UTC(y, mo - 1, d + (unit === "w" ? 7 * n : n)));
    return { y: t.getUTCFullYear(), mo: t.getUTCMonth() + 1, d: t.getUTCDate() };
  }
  if (unit === "m" || unit === "y") {
    const months = mo - 1 + (unit === "y" ? 12 * n : n);
    const ny = y + Math.floor(months / 12);
    const nmo = (months % 12) + 1;
    return { y: ny, mo: nmo, d: Math.min(d, daysInMonth(ny, nmo)) };
  }
  throw new Error(`unknown unit: ${unit}`);
}

const p2 = (v) => String(v).padStart(2, "0");

/**
 * Next due value in RemCTL's input format: 'YYYY-MM-DD' for all-day reminders,
 * 'YYYY-MM-DD HH:MM' (original time of day) for timed ones.
 */
export function nextDue({ completion, dueTime, allDay, n, unit }) {
  const next = addInterval(completion, n, unit);
  const date = `${next.y}-${p2(next.mo)}-${p2(next.d)}`;
  return allDay ? date : `${date} ${p2(dueTime.hh)}:${p2(dueTime.mm)}`;
}

/**
 * Decides what to do with one exported reminder.
 * Returns { action: 'ignore' } for untagged reminders, otherwise one of:
 *   { action: 'skip', reason }
 *   { action: 'strip-recurrence' }            open tagged reminder still carrying a native recurrence
 *   { action: 'reschedule', due, stripRecurrence, completionSource, interval }
 */
export function planReminder(r, now) {
  const { parsed, invalid } = classifyTags(r.tags);
  if (!parsed.length && !invalid.length) return { action: "ignore" };
  if (invalid.length) return { action: "skip", reason: `unparseable tag: ${invalid.join(", ")}` };
  if (parsed.length > 1) return { action: "skip", reason: `multiple after-* tags: ${parsed.map((p) => p.tag).join(", ")}` };
  const hasRecurrence = Boolean(r.recurrence);
  if (!r.completed) return hasRecurrence ? { action: "strip-recurrence" } : { action: "ignore" };

  const due = parseLocal(r.dueDate);
  if (!due) return { action: "skip", reason: "no readable due date to base the reschedule on" };
  let completion = parseLocal(r.completionDate);
  let completionSource = "remctl";
  if (!completion) {
    completion = { y: now.getFullYear(), mo: now.getMonth() + 1, d: now.getDate(), hh: now.getHours(), mm: now.getMinutes() };
    completionSource = "run-time-fallback";
  }
  const { n, unit, tag } = parsed[0];
  return {
    action: "reschedule",
    due: nextDue({ completion, dueTime: due, allDay: Boolean(r.allDay), n, unit }),
    stripRecurrence: hasRecurrence,
    completionSource,
    interval: tag,
  };
}

/** Normalises a RemCTL dueDate and an input-format due string for equality checks. */
export function sameDue(dueDate, expected, allDay) {
  const a = parseLocal(dueDate);
  const b = parseLocal(expected);
  if (!a || !b) return false;
  const day = a.y === b.y && a.mo === b.mo && a.d === b.d;
  return allDay ? day : day && a.hh === b.hh && a.mm === b.mm;
}
