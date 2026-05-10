export type AppState = "upload" | "configure" | "loading" | "reading";
export type ThemeMode = "light" | "dark" | "auto";
export type NoticeTone = "error" | "success";

export interface Notice {
  message: string;
  tone: NoticeTone;
}
