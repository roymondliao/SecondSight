const STORAGE_KEY = "secondsight:recent-projects";
const MAX_ENTRIES = 10;

export function getRecentProjects(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return [];
    }
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter((value): value is string => typeof value === "string" && value.length > 0);
  } catch {
    return [];
  }
}

export function addRecentProject(projectId: string): string[] {
  const trimmed = projectId.trim();
  if (!trimmed) {
    return getRecentProjects();
  }
  const current = getRecentProjects();
  const next = [trimmed, ...current.filter((id) => id !== trimmed)].slice(0, MAX_ENTRIES);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // localStorage can throw in private mode / when quota is exceeded — fall through silently
  }
  return next;
}
