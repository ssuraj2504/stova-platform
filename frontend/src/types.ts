export type User = { id: string; email: string; name: string };
export type Article = {
  id: string;
  title: string;
  slug: string;
  content: string;
  status: "draft" | "ready" | "published" | "error";
  wordCount: number;
  error: string | null;
  updatedAt: string;
  tag: string;
  constraints: string;
  dropCap: boolean;
};
export type Project = {
  id: string;
  name: string;
  color: string;
  logo: string | null;
  audience: string;
  tone: string;
  status: "draft" | "published";
  createdAt: string;
  updatedAt: string;
  articles: Article[];
};
