const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const timeOnlyFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
});

const dateTimeCompactFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const integerFormatter = new Intl.NumberFormat();

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "No data yet";
  }

  return dateTimeFormatter.format(new Date(value));
}

export function formatRelativeSpan(
  start: string | null | undefined,
  end: string | null | undefined,
): string {
  if (!start || !end) {
    return "No duration";
  }

  const diffMs = Math.max(new Date(end).getTime() - new Date(start).getTime(), 0);
  if (diffMs < 1_000) {
    return `${diffMs}ms`;
  }
  if (diffMs < 60_000) {
    return `${(diffMs / 1_000).toFixed(1)}s`;
  }
  return `${(diffMs / 60_000).toFixed(1)}m`;
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "0";
  }
  return integerFormatter.format(value);
}

export function formatTimeOnly(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return timeOnlyFormatter.format(new Date(value));
}

export function formatDateTimeCompact(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return dateTimeCompactFormatter.format(new Date(value));
}

export function truncateMiddle(value: string, width = 18): string {
  if (value.length <= width) {
    return value;
  }

  const side = Math.max(Math.floor((width - 3) / 2), 4);
  return `${value.slice(0, side)}...${value.slice(-side)}`;
}
