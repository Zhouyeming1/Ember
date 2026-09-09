import { readStorage, writeStorage } from "./local-storage";

export const THEMES = ["white", "paper", "dark"] as const;
export type ThemeId = (typeof THEMES)[number];
export const DEFAULT_THEME: ThemeId = "paper";
export const THEME_STORAGE_KEY = "ember.theme";

export function parseTheme(value: unknown): ThemeId {
  return THEMES.includes(value as ThemeId) ? (value as ThemeId) : DEFAULT_THEME;
}

export function readStoredTheme(): ThemeId {
  return parseTheme(readStorage(THEME_STORAGE_KEY));
}

export function applyTheme(theme: ThemeId): ThemeId {
  const next = parseTheme(theme);
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next === "dark" ? "dark" : "light";
  writeStorage(THEME_STORAGE_KEY, next);
  return next;
}
