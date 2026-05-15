import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAnalysisTrends, useDirectiveMutation, useDirectives, type Directive, type TrendsBucket } from "@/lib/api";
import { formatDateTime, formatInteger, formatSessionId, truncateMiddle } from "@/lib/format";
import { cn } from "@/lib/utils";

type DirectiveStatusVisual = { dot: string; chip: string; label: string };

function directiveStatusVisual(status: string): DirectiveStatusVisual {
  switch (status) {
    case "active":
      return { dot: "bg-emerald-500", chip: "border-emerald-300/70 bg-emerald-50 text-emerald-700", label: "active" };
    case "pending":
      return { dot: "bg-amber-500", chip: "border-amber-300/70 bg-amber-50 text-amber-700", label: "pending" };
    case "soft_disabled":
    case "disabled":
      return { dot: "bg-slate-400", chip: "border-border/70 bg-muted text-muted-foreground", label: "soft_disabled" };
    case "archived":
      return { dot: "bg-slate-300", chip: "border-border/70 bg-muted/60 text-muted-foreground", label: "archived" };
    default:
      return { dot: "bg-slate-400", chip: "border-border/70 bg-white/70 text-muted-foreground", label: status };
  }
}

function DirectiveStatusBadge({ status }: { status: string }) {
  const visual = directiveStatusVisual(status);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[11px] uppercase tracking-[0.22em]",
        visual.chip,
      )}
    >
      <span className={cn("dot", visual.dot)} />
      {visual.label}
    </span>
  );
}

const DIRECTIVE_STATUS_LEGEND: ReadonlyArray<{ status: string; note: string }> = [
  { status: "active", note: "injected each session" },
  { status: "pending", note: "awaiting confirmation" },
  { status: "soft_disabled", note: "retained, not injected" },
  { status: "archived", note: "read-only historical" },
];

type EffectivenessPoint = {
  sessionId: string;
  analyzedAt: string;
  targetCount: number;
  totalFlags: number;
};

type EffectivenessSeries = {
  directiveId: string;
  flagType: string;
  points: EffectivenessPoint[];
  injectionSessionId: string | null;
  preAverage: number | null;
  postAverage: number | null;
  preOverallAverage: number | null;
  postOverallAverage: number | null;
  preCount: number;
  postCount: number;
};

function average(values: number[]) {
  if (!values.length) {
    return null;
  }
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function totalFlags(bucket: TrendsBucket) {
  return Object.values(bucket.counts_by_type).reduce((sum, count) => sum + count, 0);
}

function formatDelta(preAverage: number | null, postAverage: number | null) {
  if (preAverage === null || postAverage === null) {
    return "Need more sessions";
  }
  if (preAverage === 0) {
    return postAverage === 0 ? "Stable at zero" : "New pattern surfaced";
  }

  const change = ((postAverage - preAverage) / preAverage) * 100;
  const prefix = change > 0 ? "+" : "";
  return `${prefix}${Math.round(change)}%`;
}

function buildEffectivenessSeries(
  directive: Directive,
  buckets: TrendsBucket[],
): EffectivenessSeries | null {
  if (!directive.source_flag_type || !buckets.length) {
    return null;
  }

  const orderedBuckets = [...buckets].sort(
    (left, right) => new Date(left.analyzed_at).getTime() - new Date(right.analyzed_at).getTime(),
  );
  const injectionTime = new Date(directive.created_at).getTime();
  const points = orderedBuckets.map((bucket) => ({
    sessionId: bucket.session_id,
    analyzedAt: bucket.analyzed_at,
    targetCount: bucket.counts_by_type[directive.source_flag_type ?? ""] ?? 0,
    totalFlags: totalFlags(bucket),
  }));

  const prePoints = points.filter((point) => new Date(point.analyzedAt).getTime() < injectionTime);
  const postPoints = points.filter((point) => new Date(point.analyzedAt).getTime() >= injectionTime);

  return {
    directiveId: directive.id,
    flagType: directive.source_flag_type,
    points,
    injectionSessionId: postPoints[0]?.sessionId ?? points.at(-1)?.sessionId ?? null,
    preAverage: average(prePoints.map((point) => point.targetCount)),
    postAverage: average(postPoints.map((point) => point.targetCount)),
    preOverallAverage: average(prePoints.map((point) => point.totalFlags)),
    postOverallAverage: average(postPoints.map((point) => point.totalFlags)),
    preCount: prePoints.length,
    postCount: postPoints.length,
  };
}

function humanizeFlagType(flagType: string) {
  return flagType.replaceAll("_", " ");
}

function formatTooltipNumber(value: unknown) {
  return typeof value === "number" ? formatInteger(value) : "0";
}

export function DirectivesView() {
  const { projectId = "" } = useParams();
  const directivesQuery = useDirectives(projectId, false);
  const trendsQuery = useAnalysisTrends(projectId);
  const mutation = useDirectiveMutation(projectId);
  const [showDisabled, setShowDisabled] = useState(true);
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const directives = useMemo(
    () =>
      (directivesQuery.data ?? []).filter((directive) =>
        showDisabled ? true : directive.status === "active",
      ),
    [directivesQuery.data, showDisabled],
  );

  const effectivenessByDirective = useMemo(() => {
    const series = new Map<string, EffectivenessSeries>();
    const buckets = trendsQuery.data?.buckets ?? [];
    for (const directive of directivesQuery.data ?? []) {
      const nextSeries = buildEffectivenessSeries(directive, buckets);
      if (nextSeries) {
        series.set(directive.id, nextSeries);
      }
    }
    return series;
  }, [directivesQuery.data, trendsQuery.data?.buckets]);

  const trackedDirectives = useMemo(
    () =>
      directives.filter((directive) => {
        const series = effectivenessByDirective.get(directive.id);
        return series && (series.preCount > 0 || series.postCount > 0);
      }).length,
    [directives, effectivenessByDirective],
  );

  return (
    <section className="space-y-4">
      <div className="section-meta">
        <p className="eyebrow">§4 · Dashboard · /projects/{projectId}/directives</p>
        <p className="section-note">
          Convention surface · lifecycle, source trace, before/after injection.
        </p>
      </div>

      <div className="grid flex-1 items-start gap-4 xl:grid-cols-[0.88fr_1.12fr]">
        <Card className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="eyebrow">Convention surface</p>
              <h2 className="text-2xl font-semibold">Directive management</h2>
            </div>
            <Button onClick={() => setShowDisabled((current) => !current)} variant="secondary">
              {showDisabled ? "Hide disabled" : "Show disabled"}
            </Button>
          </div>

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-2">
            <div className="rounded-[24px] border border-white/80 bg-white/60 p-4">
              <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                Visible directives
              </div>
              <div className="numeric mt-2 text-2xl font-semibold">{formatInteger(directives.length)}</div>
            </div>
            <div className="rounded-[24px] border border-white/80 bg-white/60 p-4">
              <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                Active
              </div>
              <div className="numeric mt-2 text-2xl font-semibold">
                {formatInteger(directives.filter((directive) => directive.status === "active").length)}
              </div>
            </div>
            <div className="rounded-[24px] border border-white/80 bg-white/60 p-4">
              <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                Disabled
              </div>
              <div className="numeric mt-2 text-2xl font-semibold">
                {formatInteger(directives.filter((directive) => directive.status === "disabled").length)}
              </div>
            </div>
            <div className="rounded-[24px] border border-white/80 bg-white/60 p-4">
              <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                Effectiveness tracked
              </div>
              <div className="numeric mt-2 text-2xl font-semibold">{formatInteger(trackedDirectives)}</div>
            </div>
          </div>

          <div className="rounded-chunk border border-dashed border-border/80 bg-white/45 p-4 text-sm leading-6 text-muted-foreground">
            Effectiveness is shown as target-flag frequency before and after the convention was created.
            The paired guardrail line is current overall flag load per session, which is the available
            task-quality proxy in this telemetry model.
          </div>

          <div className="rounded-chunk border border-white/80 bg-white/55 p-4">
            <div className="eyebrow mb-3">Status enum (per lifecycle contract)</div>
            <ul className="space-y-2 text-sm">
              {DIRECTIVE_STATUS_LEGEND.map((entry) => {
                const visual = directiveStatusVisual(entry.status);
                return (
                  <li className="flex items-center justify-between gap-3" key={entry.status}>
                    <span className="inline-flex items-center gap-2">
                      <span className={cn("dot", visual.dot)} />
                      <span className="font-mono text-xs uppercase tracking-[0.22em]">
                        {visual.label}
                      </span>
                    </span>
                    <span className="font-mono text-xs text-muted-foreground">{entry.note}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        </Card>

        <div className="space-y-4">
        {!directives.length && (
          <Card className="flex min-h-[240px] flex-col items-center justify-center gap-3 text-center">
            <p className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
              No directives yet
            </p>
            <p className="max-w-sm text-sm leading-6 text-muted-foreground">
              Directives are promoted automatically when repeated behavior patterns are detected across sessions.
            </p>
          </Card>
        )}
        {directives.map((directive) => {
          const isActive = directive.status === "active";
          const reason = reasons[directive.id] ?? "";
          const effectiveness = effectivenessByDirective.get(directive.id);

          return (
            <Card className="space-y-4" key={directive.id}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-2">
                    <DirectiveStatusBadge status={directive.status} />
                    <Badge>{directive.type}</Badge>
                    {directive.source_flag_type ? <Badge>{humanizeFlagType(directive.source_flag_type)}</Badge> : null}
                  </div>
                  <h2 className="text-xl font-semibold leading-8">{directive.instruction}</h2>
                </div>
                <div className="text-right text-xs text-muted-foreground">
                  <div className="font-mono uppercase tracking-[0.22em]">Identity</div>
                  <div className="mt-1 text-sm text-foreground">{truncateMiddle(directive.identity_key, 24)}</div>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-4">
                <div className="rounded-[22px] border border-white/80 bg-white/55 p-4">
                  <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                    Frequency
                  </div>
                  <div className="numeric mt-2 text-xl font-semibold">
                    {directive.frequency !== null ? `${Math.round(directive.frequency * 100)}%` : "n/a"}
                  </div>
                </div>
                <div className="rounded-[22px] border border-white/80 bg-white/55 p-4">
                  <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                    Confidence
                  </div>
                  <div className="numeric mt-2 text-xl font-semibold">
                    {directive.confidence !== null ? directive.confidence.toFixed(2) : "n/a"}
                  </div>
                </div>
                <div className="rounded-[22px] border border-white/80 bg-white/55 p-4">
                  <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                    Updated
                  </div>
                  <div className="mt-2 text-sm font-medium">{formatDateTime(directive.updated_at)}</div>
                </div>
                <div className="rounded-[22px] border border-white/80 bg-white/55 p-4">
                  <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                    Sources
                  </div>
                  <div className="numeric mt-2 text-xl font-semibold">
                    {formatInteger(directive.source_sessions.length)}
                  </div>
                </div>
              </div>

              <div className="space-y-3 rounded-[24px] border border-white/80 bg-white/55 p-4">
                <div className="text-sm font-medium">Source tracing</div>
                <div className="flex flex-wrap gap-2">
                  {directive.source_sessions.length ? (
                    directive.source_sessions.map((sessionId) => (
                      <Link key={sessionId} to={`/projects/${projectId}/analysis?session=${sessionId}`}>
                        <Badge className="transition-colors duration-150 hover:bg-white hover:text-foreground">
                          {formatSessionId(sessionId, 18)}
                        </Badge>
                      </Link>
                    ))
                  ) : (
                    <span className="text-sm text-muted-foreground">No source sessions recorded.</span>
                  )}
                </div>
                {directive.trigger_pattern ? (
                  <p className="text-sm leading-6 text-muted-foreground">
                    Trigger pattern: <span className="font-medium text-foreground">{directive.trigger_pattern}</span>
                  </p>
                ) : null}
                {directive.disabled_reason ? (
                  <p className="text-sm leading-6 text-muted-foreground">
                    Disabled because: <span className="font-medium text-foreground">{directive.disabled_reason}</span>
                  </p>
                ) : null}
              </div>

              {effectiveness ? (
                <div className="space-y-4 rounded-[24px] border border-white/80 bg-[linear-gradient(180deg,rgba(249,115,22,0.08),rgba(255,255,255,0.7))] p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                        Convention effectiveness
                      </p>
                      <h3 className="text-lg font-semibold">Before / after injection</h3>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Badge>{effectiveness.preCount} pre</Badge>
                      <Badge>{effectiveness.postCount} post</Badge>
                    </div>
                  </div>

                  <div className="grid gap-3 md:grid-cols-3">
                    <div className="rounded-[22px] border border-white/80 bg-white/70 p-4">
                      <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                        Target flag avg
                      </div>
                      <div className="mt-2 text-sm text-muted-foreground">
                        {effectiveness.preAverage !== null
                          ? `${effectiveness.preAverage.toFixed(1)} pre`
                          : "No pre baseline"}
                      </div>
                      <div className="numeric mt-1 text-xl font-semibold">
                        {effectiveness.postAverage !== null
                          ? `${effectiveness.postAverage.toFixed(1)} post`
                          : "No post baseline"}
                      </div>
                    </div>
                    <div className="rounded-[22px] border border-white/80 bg-white/70 p-4">
                      <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                        Effect change
                      </div>
                      <div className="numeric mt-2 text-xl font-semibold">
                        {formatDelta(effectiveness.preAverage, effectiveness.postAverage)}
                      </div>
                      <div className="mt-1 text-sm text-muted-foreground">
                        {humanizeFlagType(effectiveness.flagType)}
                      </div>
                    </div>
                    <div className="rounded-[22px] border border-white/80 bg-white/70 p-4">
                      <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
                        Overall flag load
                      </div>
                      <div className="mt-2 text-sm text-muted-foreground">
                        {effectiveness.preOverallAverage !== null
                          ? `${effectiveness.preOverallAverage.toFixed(1)} pre`
                          : "No pre baseline"}
                      </div>
                      <div className="numeric mt-1 text-xl font-semibold">
                        {effectiveness.postOverallAverage !== null
                          ? `${effectiveness.postOverallAverage.toFixed(1)} post`
                          : "No post baseline"}
                      </div>
                    </div>
                  </div>

                  <div className="h-[260px] rounded-[24px] border border-white/80 bg-white/70 p-4">
                    <ResponsiveContainer height="100%" width="100%">
                      <ComposedChart data={effectiveness.points}>
                        <defs>
                          <linearGradient id={`effectiveness-${directive.id}`} x1="0" x2="0" y1="0" y2="1">
                            <stop offset="5%" stopColor="#f97316" stopOpacity={0.3} />
                            <stop offset="95%" stopColor="#f97316" stopOpacity={0.02} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid stroke="rgba(148, 163, 184, 0.22)" vertical={false} />
                        <XAxis
                          dataKey="sessionId"
                          tickFormatter={(value) => truncateMiddle(String(value), 10)}
                          tickLine={false}
                        />
                        <YAxis allowDecimals={false} tickLine={false} width={34} />
                        <Tooltip
                          formatter={(value, name) => [
                            formatTooltipNumber(value),
                            name === "targetCount" ? humanizeFlagType(effectiveness.flagType) : "overall flag load",
                          ]}
                          labelFormatter={(value) => truncateMiddle(String(value), 18)}
                        />
                        {effectiveness.injectionSessionId ? (
                          <ReferenceLine
                            stroke="#0f172a"
                            strokeDasharray="4 4"
                            x={effectiveness.injectionSessionId}
                          />
                        ) : null}
                        <Area
                          dataKey="totalFlags"
                          fill={`url(#effectiveness-${directive.id})`}
                          fillOpacity={1}
                          name="totalFlags"
                          stroke="#f97316"
                          strokeWidth={2}
                          type="monotone"
                        />
                        <Line
                          dataKey="targetCount"
                          dot={{ fill: "#0f766e", r: 3 }}
                          name="targetCount"
                          stroke="#0f766e"
                          strokeWidth={3}
                          type="monotone"
                        />
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>

                  <div className="text-xs leading-5 text-muted-foreground">
                    The dashed marker shows the first analyzed session at or after this convention was
                    created on {formatDateTime(directive.created_at)}.
                  </div>
                </div>
              ) : directive.source_flag_type ? (
                <div className="rounded-[24px] border border-dashed border-border/80 bg-white/45 p-4 text-sm leading-6 text-muted-foreground">
                  Effectiveness will appear once the project has trend data in the current analysis window.
                </div>
              ) : null}

              {isActive ? (
                <div className="flex flex-col gap-3 sm:flex-row">
                  <Input
                    onChange={(event) =>
                      setReasons((current) => ({
                        ...current,
                        [directive.id]: event.target.value,
                      }))
                    }
                    placeholder="Why should this convention be removed?"
                    value={reason}
                  />
                  <Button
                    disabled={!reason.trim() || mutation.isPending}
                    onClick={() =>
                      mutation.mutate({
                        directiveId: directive.id,
                        patch: {
                          status: "disabled",
                          reason: reason.trim(),
                        },
                      })
                    }
                    variant="destructive"
                  >
                    Disable convention
                  </Button>
                </div>
              ) : (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-[22px] border border-border/70 bg-white/55 p-4">
                  <div>
                    <div className="text-sm font-medium">Convention disabled</div>
                    <div className="text-sm text-muted-foreground">
                      Disabled at {formatDateTime(directive.disabled_at)}
                    </div>
                  </div>
                  <Button
                    disabled={mutation.isPending}
                    onClick={() =>
                      mutation.mutate({
                        directiveId: directive.id,
                        patch: {
                          status: "active",
                        },
                      })
                    }
                    variant="secondary"
                  >
                    Re-activate
                  </Button>
                </div>
              )}

              {mutation.isError ? (
                <div className="rounded-[18px] border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
                  {mutation.error.message}
                </div>
              ) : null}
            </Card>
          );
        })}
        </div>
      </div>
    </section>
  );
}
