import type { Project } from "../types";

type Brand = Pick<Project, "name" | "logo" | "color" | "audience" | "tone">;
type Topic = { id: string; title: string; tag: string; constraints: string };
type ActionLatch = { current: boolean };

export async function runExclusiveAction<T>(latch: ActionLatch, action: () => Promise<T>): Promise<T | undefined> {
  // React state can render after another click. Acquire the ref before any await.
  if (latch.current) return undefined;
  latch.current = true;
  try { return await action(); }
  finally { latch.current = false; }
}

export function publicationHasChanges(brand: Brand, topics: Topic[], project: Project | null, emptyBrand: Brand): boolean {
  const baseline = project || emptyBrand;
  const fields: (keyof Brand)[] = ["name", "color", "logo", "audience", "tone"];
  if (fields.some(field => brand[field] !== baseline[field])) return true;
  if (!project) return topics.some(topic => !!topic.title || !!topic.tag || !!topic.constraints);
  return topics.length !== project.articles.length || topics.some((topic, index) => {
    const article = project.articles[index];
    return !article || article.id !== topic.id || article.title !== topic.title || (article.tag || "") !== topic.tag || (article.constraints || "") !== topic.constraints;
  });
}

export function canLeavePublication(dirty: boolean, confirm: () => boolean): boolean {
  return !dirty || confirm();
}

export function warnBeforeUnload(dirty: boolean, event: Pick<BeforeUnloadEvent, "preventDefault" | "returnValue">): void {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
}
