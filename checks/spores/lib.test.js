import { test } from "node:test";
import assert from "node:assert/strict";
import {
  approvalQuestion, askOutcome, checkDue, outcomeArgs, proposalSchema, reminderArgs, TAG_DONE, validateProposal,
} from "./lib.js";

const NOW = new Date(2026, 10, 6, 12, 0); // Friday 6 November 2026
const LISTS = [{ id: 6, title: "Home & Family" }, { id: 1, title: "Lee’s Jobs" }];
const reminder = (extra = {}) => ({ action: "add_reminder", title: "Clean the fridge", list: "Home & Family", due: "", reason: "Asked to.", ...extra });

test("a good reminder is resolved to its live list id", () => {
  const { ok, proposal } = validateProposal(reminder(), LISTS, NOW);
  assert.equal(ok, true);
  assert.equal(proposal.list_id, 6);
  assert.equal(proposal.due, null);
});

test("anything outside the fixed actions and live lists is refused", () => {
  assert.equal(validateProposal(null, LISTS, NOW).ok, false);
  assert.equal(validateProposal({ action: "run_command", title: "rm -rf" }, LISTS, NOW).ok, false);
  assert.equal(validateProposal(reminder({ list: "Somewhere else" }), LISTS, NOW).ok, false);
  assert.equal(validateProposal(reminder({ title: "   " }), LISTS, NOW).ok, false);
  assert.equal(validateProposal(reminder({ title: "x".repeat(201) }), LISTS, NOW).ok, false);
});

test("none needs no list or title", () => {
  const { ok, proposal } = validateProposal({ action: "none", title: "", list: "", due: "", reason: "Just an idea." }, LISTS, NOW);
  assert.equal(ok, true);
  assert.deepEqual(proposal, { action: "none", reason: "Just an idea." });
});

test("control characters can't smuggle extra lines into a title", () => {
  const { proposal } = validateProposal(reminder({ title: "Clean\nthe\u0007 fridge" }), LISTS, NOW);
  assert.equal(proposal.title, "Clean the fridge");
});

test("due dates must be real, well formed and not past", () => {
  assert.equal(checkDue("2026-11-06", NOW), null);
  assert.equal(checkDue("2026-11-07 09:30", NOW), null);
  assert.match(checkDue("tomorrow", NOW), /not YYYY-MM-DD/);
  assert.match(checkDue("2026-02-30", NOW), /not a real date/);
  assert.match(checkDue("2026-11-07 25:00", NOW), /not a real date/);
  assert.match(checkDue("2026-11-05", NOW), /in the past/);
});

test("the schema only offers the live lists, or none", () => {
  assert.deepEqual(proposalSchema(["A", "B"]).properties.list.enum, ["A", "B", ""]);
});

test("the approval card says what will happen and quotes the spore", () => {
  const { proposal } = validateProposal(reminder({ due: "2026-11-07 09:30" }), LISTS, NOW);
  const question = approvalQuestion({ id: 157, source: "dictation", text: "Remind me to clean the fridge." }, proposal);
  assert.equal(question, 'Add to Home & Family, due Sat 7 Nov at 09:30: "Clean the fridge"?\n\nSpore 157: "Remind me to clean the fridge."');
});

test("the card names a source that isn't Lee's own voice", () => {
  const { proposal } = validateProposal(reminder(), LISTS, NOW);
  assert.match(approvalQuestion({ id: 9, source: "share", text: "x" }, proposal), /Spore 9 from share:/);
});

test("the title always comes after --, so it can't be read as an option", () => {
  const { proposal } = validateProposal(reminder({ title: "--list-id 9", due: "2026-11-07" }), LISTS, NOW);
  assert.deepEqual(reminderArgs(proposal), ["add", "--list-id", "6", "--json", "--due", "2026-11-07", "--", "--list-id 9"]);
});

test("a new outcome replaces tags from earlier attempts", () => {
  const args = outcomeArgs(TAG_DONE);
  assert.deepEqual(args.slice(0, 2), ["--add-tag", "rhizomorph"]);
  assert.equal(args.filter((a) => a === "--remove-tag").length, 3);
  assert.equal(args.slice(2).includes("rhizomorph"), false);
});

test("only exit 0 approves", () => {
  assert.equal(askOutcome(0), "approved");
  assert.equal(askOutcome(1), "declined");
  assert.equal(askOutcome(3), "timed_out");
  assert.equal(askOutcome(2), "unreachable");
  assert.equal(askOutcome(4), "unreachable");
});
