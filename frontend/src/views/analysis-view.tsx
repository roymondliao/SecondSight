import { useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  useAnalysisAggregation,
  useAnalysisDetail,
  useAnalysisSessions,
  useAnalysisSummary,
  useAnalysisTrends,
  useDirectives,
  type BehaviorFlag,
  type Directive,
} from "@/lib/api";
import { formatDateTime, formatInteger, formatSessionId, truncateMiddle } from "@/lib/format";
import { cn } from "@/lib/utils";

type FlagSeverityVisual = { dot: string; chip: string };

function flagSeverityVisual(confidence: BehaviorFlag["confidence"]): FlagSeverityVisual {
  switch (confidence) {
    case "high":
      return {
        dot: "bg-destructive",
        chip: "border-destructive/30 bg-destructive/10 text-destructive",
      };
    case "medium":
      return {
        dot: "bg-accent",
        chip: "border-accent/30 bg-accent/10 text-accent",
      };
    case "low":
      return {
        dot: "bg-muted-foreground/70",
        chip: "border-border/70 bg-muted text-muted-foreground",
      };
  }
}

const SERIES_COLORS = ["#0f766e", "#f97316", "#0284c7", "#dc2626", "#7c3aed", "#16a34a"];

type DistributionSlice = {
  flagType: string;
  flagCount: number;
  sessionCount: number;
  share: number;
};

type PatternGroup = {
  flagType: string;
  flagCount: number;
  sessionCount: number;
  directives: Directive[];
};

function groupFlagsBySegment(flags: BehaviorFlag[]) {
  return flags.reduce<Record<number, BehaviorFlag[]>>((accumulator, flag) => {
    accumulator[flag.segment_index] ??= [];
    accumulator[flag.segment_index].push(flag);
    return accumulator;
  }, {});
}

function humanizeFlagType(flagType: string) {
  return flagType.replaceAll("_", " ");
}

function buildDistribution(
  flagCounts: Record<string, number>,
  sessionCounts: Record<string, number>,
): DistributionSlice[] {
  const total = Object.values(flagCounts).reduce((sum, count) => sum + count, 0);
  return Object.entries(flagCounts)
    .map(([flagType, flagCount]) => ({
      flagType,
      flagCount,
      sessionCount: sessionCounts[flagType] ?? 0,
      share: total > 0 ? flagCount / total : 0,
    }))
    .sort((left, right) => right.flagCount - left.flagCount);
}

function groupPatterns(
  directives: Directive[],
  flagCounts: Record<string, number>,
  sessionCounts: Record<string, number>,
): PatternGroup[] {
  const grouped = new Map<string, Directive[]>();

  for (const directive of directives) {
    if (!directive.source_flag_type) {
      continue;
    }
    grouped.set(directive.source_flag_type, [
      ...(grouped.get(directive.source_flag_type) ?? []),
      directive,
    ]);
  }

  return Array.from(grouped.entries())
    .map(([flagType, items]) => ({
      flagType,
      flagCount: flagCounts[flagType] ?? 0,
      sessionCount: sessionCounts[flagType] ?? 0,
      directives: items.sort((left, right) => {
        const leftFrequency = left.frequency ?? -1;
        const rightFrequency = right.frequency ?? -1;
        if (leftFrequency !== rightFrequency) {
          return rightFrequency - leftFrequency;
        }
        return right.updated_at.localeCompare(left.updated_at);
      }),
    }))
    .sort((left, right) => {
      if (left.flagCount !== right.flagCount) {
        return right.flagCount - left.flagCount;
      }
      return right.directives.length - left.directives.length;
    });
}

function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function formatTooltipNumber(value: unknown) {
  return typeof value === "number" ? formatInteger(value) : "0";
}

function hasTrendData(rows: Array<Record<string, number | string>>) {
  return rows.some((row) =>
    Object.entries(row).some(([key, value]) => key !== "session" && typeof value === "number" && value > 0),
  );
}

export function AnalysisView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { projectId = "" } = useParams();
  const summaryQuery = useAnalysisSummary(projectId);
  const sessionsQuery = useAnalysisSessions(projectId);
  const trendsQuery = useAnalysisTrends(projectId);
  const aggregationQuery = useAnalysisAggregation(projectId);
  const directivesQuery = useDirectives(projectId, false);

  const selectedSessionId =
    searchParams.get("session") ?? sessionsQuery.data?.items[0]?.session_id ?? null;
  const detailQuery = useAnalysisDetail(projectId, selectedSessionId);

  const trendSeries = useMemo(() => {
    const buckets = trendsQuery.data?.buckets ?? [];
    const flagTypes = Array.from(
      new Set(buckets.flatMap((bucket) => Object.keys(bucket.counts_by_type))),
    );
    return {
      flagTypes,
      rows: buckets.map((bucket) => ({
        session: bucket.session_id,
        ...bucket.counts_by_type,
      })),
    };
  }, [trendsQuery.data?.buckets]);

  const groupedFlags = useMemo(
    () => groupFlagsBySegment(detailQuery.data?.flags ?? []),
    [detailQuery.data?.flags],
  );

  const distribution = useMemo(
    () =>
      buildDistribution(
        aggregationQuery.data?.flag_counts_by_type ?? {},
        aggregationQuery.data?.session_counts_by_type ?? {},
      ),
    [aggregationQuery.data?.flag_counts_by_type, aggregationQuery.data?.session_counts_by_type],
  );

  const patternGroups = useMemo(
    () =>
      groupPatterns(
        directivesQuery.data ?? [],
        aggregationQuery.data?.flag_counts_by_type ?? {},
        aggregationQuery.data?.session_counts_by_type ?? {},
      ),
    [
      directivesQuery.data,
      aggregationQuery.data?.flag_counts_by_type,
      aggregationQuery.data?.session_counts_by_type,
    ],
  );

  const trendDataAvailable = hasTrendData(trendSeries.rows);

  return (
    <section className="space-y-4">
      <div className="section-meta">
        <p className="eyebrow">§3 · Dashboard · /projects/{projectId}/analysis</p>
        <p className="section-note">
          Stat strip + analysis list + flag trends. Accent reserved for charts.
        </p>
      </div>

      <div className="grid flex-1 items-start gap-4 xl:grid-cols-[1.02fr_1.18fr_1fr]">
        <div className="space-y-4">
          <Card className="grid gap-3 sm:grid-cols-3 xl:grid-cols-1">
            <div>
              <div className="eyebrow">Sessions analyzed</div>
              <div className="numeric mt-2 text-3xl font-semibold">
                {formatInteger(summaryQuery.data?.analyzed_session_count)}
              </div>
            </div>
            <div>
              <div className="eyebrow">Active conventions</div>
              <div className="numeric mt-2 text-3xl font-semibold">
                {formatInteger(summaryQuery.data?.active_directive_count)}
              </div>
            </div>
            <div>
              <div className="eyebrow">Last analyzed</div>
              <div className="mt-2 text-sm font-medium">
                {formatDateTime(summaryQuery.data?.last_analyzed_at)}
              </div>
            </div>
          </Card>

          <Card className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                  Session reports
                </p>
                <h2 className="text-2xl font-semibold">Analysis list</h2>
              </div>
              <Badge>{formatInteger(sessionsQuery.data?.items.length ?? 0)} loaded</Badge>
            </div>
            <div className="space-y-3">
              {!sessionsQuery.data?.items.length && (
                <div className="rounded-[24px] border border-dashed border-border/80 bg-white/45 p-5 text-sm leading-6 text-muted-foreground">
                  No analyzed sessions yet. Analysis runs automatically after sessions are ingested — check back after your first full session completes.
                </div>
              )}
              {sessionsQuery.data?.items.map((session) => {
                const active = session.session_id === selectedSessionId;
                return (
                  <button
                    className={`w-full rounded-[24px] border p-4 text-left transition-all duration-150 ${
                      active
                        ? "border-primary/35 bg-primary text-primary-foreground shadow-sm"
                        : "border-white/70 bg-white/60 hover:bg-white"
                    }`}
                    key={session.session_id}
                    onClick={() => setSearchParams({ session: session.session_id })}
                  >
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <div className="font-mono text-xs">
                        {formatSessionId(session.session_id, 18)}
                      </div>
                    <Badge className={active ? "border-white/20 bg-white/15 text-white" : ""}>
                      {session.flag_count} flags
                    </Badge>
                  </div>
                  <p className="mb-2 text-base font-medium">{session.headline}</p>
                  <div className="mb-2 text-xs text-inherit/80">{formatDateTime(session.analyzed_at)}</div>
                  <div className="flex flex-wrap gap-2">
                    {session.key_findings.map((finding) => (
                      <span
                        className={`inline-flex items-center rounded-full border px-2.5 py-0.5 font-mono text-[11px] ${
                          active
                            ? "border-white/25 bg-white/15 text-white"
                            : "border-border/70 bg-slate-50 text-muted-foreground"
                        }`}
                        key={finding}
                      >
                        {finding}
                      </span>
                    ))}
                  </div>
                </button>
              );
            })}
          </div>
        </Card>
      </div>

      <Card className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
              Project-level summary
            </p>
            <h2 className="text-2xl font-semibold">Flag trends</h2>
          </div>
          <Badge>{formatDateTime(summaryQuery.data?.as_of)}</Badge>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {Object.entries(summaryQuery.data?.flag_counts_by_type ?? {}).map(([flagType, count]) => (
            <div className="rounded-[24px] border border-white/80 bg-white/65 p-4" key={flagType}>
              <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                {humanizeFlagType(flagType)}
              </div>
              <div className="numeric mt-2 text-2xl font-semibold">{count}</div>
            </div>
          ))}
        </div>
        <div className="h-[340px] rounded-[28px] border border-white/80 bg-[linear-gradient(180deg,rgba(14,165,233,0.06),rgba(255,255,255,0.75))] p-4">
          {trendDataAvailable ? (
            <ResponsiveContainer height="100%" width="100%">
              <AreaChart data={trendSeries.rows}>
                <defs>
                  {trendSeries.flagTypes.map((flagType, index) => (
                    <linearGradient id={`gradient-${flagType}`} key={flagType} x1="0" x2="0" y1="0" y2="1">
                      <stop offset="5%" stopColor={SERIES_COLORS[index % SERIES_COLORS.length]} stopOpacity={0.5} />
                      <stop offset="95%" stopColor={SERIES_COLORS[index % SERIES_COLORS.length]} stopOpacity={0.02} />
                    </linearGradient>
                  ))}
                </defs>
                <CartesianGrid stroke="rgba(148, 163, 184, 0.25)" vertical={false} />
                <XAxis dataKey="session" tickFormatter={(value) => truncateMiddle(String(value), 12)} tickLine={false} />
                <YAxis allowDecimals={false} tickLine={false} width={34} />
                <Tooltip
                  formatter={(value, name) => [formatTooltipNumber(value), humanizeFlagType(String(name))]}
                  labelFormatter={(value) => truncateMiddle(String(value), 18)}
                />
                {trendSeries.flagTypes.map((flagType, index) => (
                  <Area
                    dataKey={flagType}
                    fill={`url(#gradient-${flagType})`}
                    key={flagType}
                    stackId="flags"
                    stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
                    strokeWidth={2}
                    type="monotone"
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center rounded-[24px] border border-dashed border-border/70 bg-white/45 px-6 text-center">
              <div className="space-y-3">
                <div className="eyebrow">Trend chart idle</div>
                <div className="text-lg font-medium">No session trend data yet</div>
                <p className="max-w-md text-sm leading-6 text-muted-foreground">
                  The chart activates after analysis has run across at least one ingested session.
                </p>
              </div>
            </div>
          )}
        </div>

        <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
          <div className="rounded-[28px] border border-white/80 bg-white/60 p-4">
            <div className="mb-4 flex items-center justify-between">
              <div className="min-w-0">
                <p className="truncate font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                  Cross-session distribution
                </p>
                <h3 className="text-xl font-semibold">Flag type mix</h3>
              </div>
              <Badge>{formatInteger(distribution.length)} {distribution.length === 1 ? "slice" : "slices"}</Badge>
            </div>

            {distribution.length ? (
              <>
                <div className="h-[220px]">
                  <ResponsiveContainer height="100%" width="100%">
                    <PieChart>
                      <Pie
                        cx="50%"
                        cy="50%"
                        data={distribution}
                        dataKey="flagCount"
                        innerRadius={54}
                        outerRadius={84}
                        paddingAngle={3}
                      >
                        {distribution.map((slice, index) => (
                          <Cell fill={SERIES_COLORS[index % SERIES_COLORS.length]} key={slice.flagType} />
                        ))}
                      </Pie>
                      <Tooltip
                        formatter={(value, _name, entry) => {
                          const payload = entry.payload as DistributionSlice;
                          return [
                            `${formatTooltipNumber(value)} flags across ${formatInteger(payload.sessionCount)} sessions`,
                            humanizeFlagType(payload.flagType),
                          ];
                        }}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
                <div className="space-y-3">
                  {distribution.map((slice, index) => (
                    <div className="flex items-center justify-between gap-3" key={slice.flagType}>
                      <div className="flex items-center gap-3">
                        <span
                          className="h-3 w-3 rounded-full"
                          style={{ backgroundColor: SERIES_COLORS[index % SERIES_COLORS.length] }}
                        />
                        <div>
                          <div className="text-sm font-medium">{humanizeFlagType(slice.flagType)}</div>
                          <div className="text-xs text-muted-foreground">
                            {formatInteger(slice.sessionCount)} sessions involved
                          </div>
                        </div>
                      </div>
                      <div className="text-right">
                        <div className="numeric text-sm font-semibold">{formatInteger(slice.flagCount)}</div>
                        <div className="text-xs text-muted-foreground">{formatPercent(slice.share)}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="rounded-[24px] border border-dashed border-border/80 bg-white/45 p-5 text-sm leading-6 text-muted-foreground">
                Run project analysis on more than one session to unlock the cross-session mix view.
              </div>
            )}
          </div>

          <div className="rounded-[28px] border border-white/80 bg-white/60 p-4">
            <div className="mb-4 flex items-center justify-between">
              <div className="min-w-0">
                <p className="truncate font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                  Behavior pattern groups
                </p>
                <h3 className="text-xl font-semibold">Promoted conventions</h3>
              </div>
              <Button asChild size="sm" variant="secondary">
                <Link to={`/projects/${projectId}/directives`}>Open directives</Link>
              </Button>
            </div>

            <div className="space-y-3">
              {patternGroups.length ? (
                patternGroups.map((group) => (
                  <div className="rounded-[24px] border border-white/80 bg-white/75 p-4" key={group.flagType}>
                    <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                          {humanizeFlagType(group.flagType)}
                        </div>
                        <div className="mt-2 text-lg font-semibold">
                          {formatInteger(group.directives.length)} convention patterns promoted
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Badge>{formatInteger(group.flagCount)} flags</Badge>
                        <Badge>{formatInteger(group.sessionCount)} sessions</Badge>
                      </div>
                    </div>

                    <div className="space-y-2">
                      {group.directives.slice(0, 3).map((directive) => (
                        <div className="rounded-[20px] border border-border/70 bg-white px-4 py-3" key={directive.id}>
                          <div className="mb-2 flex flex-wrap items-center gap-2">
                            <Badge>{directive.status}</Badge>
                            <Badge>{directive.frequency !== null ? formatPercent(directive.frequency) : "n/a"}</Badge>
                          </div>
                          <div className="text-sm font-medium leading-6">{directive.instruction}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))
              ) : (
                <div className="rounded-[24px] border border-dashed border-border/80 bg-white/45 p-5 text-sm leading-6 text-muted-foreground">
                  No cross-session convention groups yet. They appear after aggregation promotes
                  repeated patterns into directives.
                </div>
              )}
            </div>
          </div>
        </div>
      </Card>

      <Card className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
              Per-session report
            </p>
            <h2 className="text-2xl font-semibold">
              {selectedSessionId
                ? formatSessionId(selectedSessionId, 20)
                : sessionsQuery.data?.items.length
                  ? "Select a session"
                  : "No sessions analyzed"}
            </h2>
          </div>
          {selectedSessionId ? (
            <Button asChild variant="secondary">
              <Link to={`/projects/${projectId}/observation?session=${selectedSessionId}`}>
                Open observation
              </Link>
            </Button>
          ) : null}
        </div>

        {detailQuery.data ? (
          <>
            <div className="rounded-[24px] border border-white/80 bg-white/60 p-4">
              <div className="mb-2 text-sm text-muted-foreground">
                {formatDateTime(detailQuery.data.analyzed_at)}
              </div>
              <p className="text-lg font-medium">{detailQuery.data.headline}</p>
              <p className="mt-3 text-sm leading-7 text-muted-foreground">{detailQuery.data.body}</p>
            </div>
            <div className="space-y-3">
              {Object.entries(groupedFlags).map(([segmentIndex, flags]) => (
                <div className="space-y-3 rounded-chunk border border-white/80 bg-white/60 p-4" key={segmentIndex}>
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                      Segment {segmentIndex}
                    </span>
                    <Button asChild size="sm" variant="ghost">
                      <Link
                        to={`/projects/${projectId}/observation?session=${detailQuery.data.session_id}&segment=${segmentIndex}`}
                      >
                        Trace segment
                      </Link>
                    </Button>
                  </div>
                  <div className="space-y-3">
                    {flags.map((flag) => {
                      const visual = flagSeverityVisual(flag.confidence);
                      return (
                        <div className="rounded-chunk border border-white/80 bg-white/65 p-4" key={flag.id}>
                          <div className="mb-2 flex flex-wrap gap-2">
                            <span
                              className={cn(
                                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[11px] uppercase tracking-[0.22em]",
                                visual.chip,
                              )}
                            >
                              <span className={cn("dot", visual.dot)} />
                              {humanizeFlagType(flag.flag_type)}
                            </span>
                            <span className="inline-flex items-center rounded-full border border-border/70 bg-white px-2.5 py-1 font-mono text-[11px] uppercase tracking-[0.22em] text-muted-foreground">
                              conf · {flag.confidence}
                            </span>
                            <span className="inline-flex items-center rounded-full border border-border/70 bg-white px-2.5 py-1 font-mono text-[11px] uppercase tracking-[0.22em] text-muted-foreground">
                              evt · {flag.event_ids.length}
                            </span>
                          </div>
                          <p className="text-sm font-medium leading-6">{flag.intent_summary}</p>
                          <p className="mt-2 text-xs leading-5 text-muted-foreground">
                            <span className="font-mono uppercase tracking-[0.22em]">Reason</span> · {flag.reason}
                          </p>
                          {flag.event_ids.length > 0 ? (
                            <div className="mt-3 flex flex-wrap gap-1.5">
                              {flag.event_ids.slice(0, 4).map((eventId) => (
                                <span
                                  className="inline-flex items-center rounded-full border border-border/60 bg-slate-50 px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
                                  key={eventId}
                                  title={eventId}
                                >
                                  {truncateMiddle(eventId, 16)}
                                </span>
                              ))}
                              {flag.event_ids.length > 4 ? (
                                <span className="inline-flex items-center rounded-full border border-border/60 bg-slate-50 px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                                  +{flag.event_ids.length - 4}
                                </span>
                              ) : null}
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="rounded-[28px] border border-dashed border-border/80 bg-white/45 p-6 text-sm leading-6 text-muted-foreground">
            {sessionsQuery.data?.items.length
              ? "Pick a session report on the left to reveal grouped behavior flags and their trace links."
              : "Analyzed session reports will appear here once analysis has run on at least one session."}
          </div>
        )}
        </Card>
        </div>
    </section>
  );
}
