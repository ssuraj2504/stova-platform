import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({ configFile: false, envDir: false, server: { middlewareMode: true, hmr: false }, appType: "custom" });
const { runExclusiveAction, publicationHasChanges, canLeavePublication, warnBeforeUnload } = await server.ssrLoadModule("/src/lib/publication-work.ts");
after(async () => { await server.close(); });

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test("rapid generate and save clicks share one operation across connection, save, and the full queue", async () => {
  const latch = { current: false };
  const connection = deferred(), saved = deferred(), firstArticle = deferred(), secondArticle = deferred();
  const atSave = deferred(), atFirstArticle = deferred(), atSecondArticle = deferred();
  let publications = 0, articles = 0, extraSaves = 0;
  const generate = () => runExclusiveAction(latch, async () => {
    await connection.promise;
    publications += 1; atSave.resolve();
    await saved.promise;
    articles += 1; atFirstArticle.resolve();
    await firstArticle.promise;
    articles += 1; atSecondArticle.resolve();
    await secondArticle.promise;
    return "complete";
  });
  const save = () => runExclusiveAction(latch, async () => { extraSaves += 1; });
  const running = generate();
  assert.equal(latch.current, true, "acquired before the first connection await");
  assert.equal(await generate(), undefined);
  await save();
  connection.resolve(); await atSave.promise;
  await generate(); await save();
  saved.resolve(); await atFirstArticle.promise;
  await generate(); await save();
  firstArticle.resolve(); await atSecondArticle.promise;
  await generate(); await save();
  assert.equal(latch.current, true, "still held while the second article writes");
  secondArticle.resolve();
  assert.equal(await running, "complete");
  assert.deepEqual({ publications, articles, extraSaves }, { publications: 1, articles: 2, extraSaves: 0 });
  assert.equal(latch.current, false);
  await save();
  assert.equal(extraSaves, 1, "a later intentional save can proceed");
});

test("the operation is released after setup refusal, failed saving, and failed writing", async () => {
  const latch = { current: false };
  assert.equal(await runExclusiveAction(latch, async () => null), null);
  assert.equal(latch.current, false);
  for (const stage of ["save", "write"]) {
    await assert.rejects(runExclusiveAction(latch, async () => { await Promise.resolve(); throw new Error(stage); }), new RegExp(stage));
    assert.equal(latch.current, false);
    assert.equal(await runExclusiveAction(latch, async () => "retry"), "retry");
  }
});

const empty = { name: "", color: "#1B2A4A", logo: null, audience: "", tone: "Warm & approachable" };
const topics = [{ id: "one", title: "First topic", tag: "News", constraints: "Useful examples" }, { id: "two", title: "Second topic", tag: "", constraints: "" }];
const brand = { ...empty, name: "Example Company" };
const project = { ...brand, articles: topics.map(topic => ({ ...topic })) };

test("all unsaved brand fields and topic notes are protected, even before entering a company name", () => {
  const blankTopics = [{ id: "new", title: "", tag: "", constraints: "" }];
  assert.equal(publicationHasChanges(empty, blankTopics, null, empty), false);
  for (const patch of [{ name: "Brand" }, { color: "#FFFFFF" }, { logo: "data:image/png;base64,example" }, { audience: "Readers" }, { tone: "Professional" }]) {
    assert.equal(publicationHasChanges({ ...empty, ...patch }, blankTopics, null, empty), true);
  }
  for (const patch of [{ title: "A topic" }, { tag: "News" }, { constraints: "Cover this detail" }]) {
    assert.equal(publicationHasChanges(empty, [{ ...blankTopics[0], ...patch }], null, empty), true);
  }
});

test("saving clears the leave warning and topic reordering or edits make it dirty again", () => {
  assert.equal(publicationHasChanges(brand, topics, project, empty), false);
  assert.equal(publicationHasChanges(brand, [...topics].reverse(), project, empty), true);
  assert.equal(publicationHasChanges(brand, [{ ...topics[0], constraints: "Changed brief" }, topics[1]], project, empty), true);
  assert.equal(publicationHasChanges(brand, topics.slice(0, 1), project, empty), true);
  const editedTopics = [{ ...topics[0], title: "Renamed topic" }, topics[1]];
  const savedProject = { ...project, articles: editedTopics.map(topic => ({ ...topic })) };
  const dirtyAfterSave = publicationHasChanges(brand, editedTopics, savedProject, empty);
  assert.equal(dirtyAfterSave, false);
  assert.equal(canLeavePublication(dirtyAfterSave, () => { throw new Error("An intentional save must not prompt"); }), true);
});

test("cancelled switching preserves the working draft; confirmed switching may leave it", () => {
  const workingCopy = { ...brand, name: "Unsaved brand" };
  let current = workingCopy;
  let prompts = 0;
  const changePublication = answer => {
    if (canLeavePublication(publicationHasChanges(current, topics, project, empty), () => { prompts += 1; return answer; })) current = empty;
  };
  changePublication(false);
  assert.equal(current, workingCopy);
  changePublication(true);
  assert.equal(current, empty);
  assert.equal(prompts, 2);
});

test("browser leave is prevented only while the publication has unsaved changes", () => {
  let prevented = 0;
  const event = { preventDefault() { prevented += 1; }, returnValue: "unchanged" };
  warnBeforeUnload(false, event);
  assert.deepEqual({ prevented, value: event.returnValue }, { prevented: 0, value: "unchanged" });
  warnBeforeUnload(true, event);
  assert.deepEqual({ prevented, value: event.returnValue }, { prevented: 1, value: "" });
});
