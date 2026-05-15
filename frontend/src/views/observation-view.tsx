import { useMemo } from "react";
import { ChevronRight, Clock3, Layers3, Sparkles } from "lucide-react";
import { useParams, useSearchParams } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import {
  formatDateTimeCompact,
  formatInteger,
  formatRelativeSpan,
  formatSessionId,
  formatTimeOnly,
  truncateMiddle,
} from "@/lib/format";
import { useObservationSessions, useSegmentDetail, useSegments } from "@/lib/api";

function eventTypeBadgeClass(eventType: string): string {
  switch (eventType) {
    case "user_prompt":
      return "border-primary/20 bg-primary/10 text-primary";
    case "tool_use_start":
      return "border-amber-200 bg-amber-50 text-amber-700";
    case "tool_use_end":
      return "border-teal-200 bg-teal-50 text-teal-700";
    case "session_start":
      return "border-sky-200 bg-sky-50 text-sky-700";
    case "session_end":
      return "border-slate-200 bg-slate-50 text-slate-600";
    case "thinking":
      return "border-violet-200 bg-violet-50 text-violet-700";
    case "agent_response":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    case "subagent_start":
      return "border-orange-200 bg-orange-50 text-orange-700";
    case "subagent_end":
      return "border-rose-200 bg-rose-50 text-rose-700";
    case "turn_start":
      return "border-cyan-200 bg-cyan-50 text-cyan-700";
    case "turn_end":
      return "border-indigo-200 bg-indigo-50 text-indigo-700";
    default:
      return "";
  }
}

function eventCardClass(eventType: string): string {
  switch (eventType) {
    case "tool_use_start":
      return "border-amber-200/90 bg-[linear-gradient(180deg,rgba(254,243,199,0.55),rgba(255,255,255,0.82))]";
    case "tool_use_end":
      return "border-teal-200/90 bg-[linear-gradient(180deg,rgba(204,251,241,0.55),rgba(255,255,255,0.82))]";
    case "session_start":
      return "border-sky-200/90 bg-[linear-gradient(180deg,rgba(224,242,254,0.65),rgba(255,255,255,0.82))]";
    case "session_end":
      return "border-slate-200/90 bg-[linear-gradient(180deg,rgba(241,245,249,0.72),rgba(255,255,255,0.86))]";
    case "thinking":
      return "border-violet-200/90 bg-[linear-gradient(180deg,rgba(245,243,255,0.7),rgba(255,255,255,0.84))]";
    case "agent_response":
      return "border-emerald-200/90 bg-[linear-gradient(180deg,rgba(236,253,245,0.68),rgba(255,255,255,0.84))]";
    default:
      return "border-white/80 bg-white/70";
  }
}

function humanizeEventType(eventType: string): string {
  return eventType.replaceAll("_", " ").replaceAll("subagent", "sub-agent");
}

function EmptyPanel({
  eyebrow,
  title,
  body,
}: {
  eyebrow: string;
  title: string;
  body: string;
}) {
  return (
    <Card className="min-h-[160px] space-y-3 border-dashed border-border/80 bg-white/45">
      <p className="eyebrow">{eyebrow}</p>
      <h3 className="text-xl font-medium">{title}</h3>
      <p className="max-w-md text-sm leading-6 text-muted-foreground">{body}</p>
    </Card>
  );
}

export function ObservationView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { projectId = "" } = useParams();
  const sessionsQuery = useObservationSessions(projectId);

  const selectedSessionId =
    searchParams.get("session") ?? sessionsQuery.data?.sessions[0]?.session_id ?? null;

  const segmentsQuery = useSegments(projectId, selectedSessionId);
  const selectedSegmentIndex = useMemo(() => {
    const raw = searchParams.get("segment");
    if (raw !== null) {
      const parsed = Number(raw);
      if (!Number.isNaN(parsed)) {
        return parsed;
      }
    }
    return segmentsQuery.data?.segments[0]?.segment_index ?? null;
  }, [searchParams, segmentsQuery.data?.segments]);

  const segmentDetailQuery = useSegmentDetail(projectId, selectedSessionId, selectedSegmentIndex);

  return (
    <section className="space-y-4">
      <div className="section-meta">
        <p className="eyebrow">§2 · Dashboard · /projects/{projectId}/observation</p>
        <p className="section-note">
          Three-column ladder: Sessions → Segments → Events. Spatial truth.
        </p>
      </div>

      <div className="grid flex-1 items-start gap-4 xl:grid-cols-[1.05fr_0.95fr_1.3fr] [&>*]:min-w-0">
        <Card className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="eyebrow">Level 1</p>
              <h2 className="text-2xl font-semibold">Sessions</h2>
            </div>
            <Badge variant="category" className="numeric">
              {formatInteger(sessionsQuery.data?.sessions.length ?? 0)} loaded
            </Badge>
          </div>
          <div className="space-y-3">
            {sessionsQuery.data?.sessions.length ? (
              sessionsQuery.data.sessions.map((session) => {
                const active = session.session_id === selectedSessionId;
                return (
                  <button
                    className={`w-full rounded-[24px] border p-4 text-left transition-all duration-150 ${
                      active
                        ? "border-primary/35 bg-primary text-primary-foreground shadow-sm"
                        : "border-white/70 bg-white/60 hover:bg-white"
                    }`}
                    key={session.session_id}
                    onClick={() =>
                      setSearchParams({
                        session: session.session_id,
                      })
                    }
                  >
                    <div className="mb-3 flex items-center justify-between gap-3">
                      <div className="font-mono text-xs" title={session.session_id}>
                        {formatSessionId(session.session_id, 20)}
                      </div>
                      <ChevronRight className="h-4 w-4" />
                    </div>
                    <div className="grid grid-cols-2 gap-3 text-xs">
                      <div>
                        <div className="opacity-70">Events</div>
                        <div className="numeric mt-1 text-sm font-medium">
                          {formatInteger(session.event_count)}
                        </div>
                      </div>
                      <div>
                        <div className="opacity-70">Segments</div>
                        <div className="numeric mt-1 text-sm font-medium">
                          {formatInteger(session.segment_count)}
                        </div>
                      </div>
                      <div>
                        <div className="opacity-70">Started</div>
                        <div className="mt-1 truncate text-sm font-medium">
                          {formatDateTimeCompact(session.first_event_at)}
                        </div>
                      </div>
                      <div>
                        <div className="opacity-70">Span</div>
                        <div className="numeric mt-1 text-sm font-medium">
                          {formatRelativeSpan(session.first_event_at, session.last_event_at)}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })
            ) : (
              <EmptyPanel
                body="Observation data appears here as soon as a project has ingested sessions."
                eyebrow="Idle"
                title={sessionsQuery.isLoading ? "Loading sessions" : "No sessions found"}
              />
            )}
          </div>
        </Card>

        <Card className="space-y-4">
          <div className="space-y-3">
            <div>
              <p className="eyebrow">Level 2</p>
              <h2 className="text-2xl font-semibold">Segments</h2>
            </div>
            {selectedSessionId ? (
              <div
                className="chip-pill chip-mono max-w-full border-primary/30 bg-primary/10 text-primary"
                title={selectedSessionId}
              >
                <span className="opacity-60">Session</span>
                <span className="normal-case tracking-normal">
                  {formatSessionId(selectedSessionId, 20)}
                </span>
              </div>
            ) : (
              <Badge variant="category">No session</Badge>
            )}
          </div>
          <div className="space-y-3">
            {segmentsQuery.data?.segments.length ? (
              segmentsQuery.data.segments.map((segment) => {
                const active = segment.segment_index === selectedSegmentIndex;
                return (
                  <button
                    className={`w-full rounded-[24px] border p-4 text-left transition-all duration-150 ${
                      active
                        ? "border-primary/30 bg-primary/12 text-primary shadow-sm"
                        : "border-white/70 bg-white/60 hover:bg-white"
                    }`}
                    key={`${segment.session_id}-${segment.segment_index}`}
                    onClick={() =>
                      setSearchParams({
                        session: segment.session_id,
                        segment: String(segment.segment_index),
                      })
                    }
                  >
                    <div className="mb-3 flex items-center justify-between">
                      <div className="inline-flex items-center gap-2">
                        <Layers3 className="h-4 w-4" />
                        <span className="font-medium">Segment {segment.segment_index}</span>
                      </div>
                      <span className="font-mono text-xs uppercase tracking-[0.22em]">
                        {formatInteger(segment.event_count)} events
                      </span>
                    </div>
                    <div className="grid grid-cols-2 gap-3 text-xs">
                      <div>
                        <div className="opacity-70">Tokens</div>
                        <div className="numeric mt-1 text-sm font-medium">
                          {formatInteger(segment.token_count)}
                        </div>
                      </div>
                      <div>
                        <div className="opacity-70">Duration</div>
                        <div className="numeric mt-1 text-sm font-medium">
                          {segment.duration_ms ? `${segment.duration_ms}ms` : "n/a"}
                        </div>
                      </div>
                      <div className="col-span-2">
                        <div className="opacity-70">Started</div>
                        <div className="mt-1 truncate text-sm font-medium">
                          {formatDateTimeCompact(segment.first_event_at)}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })
            ) : (
              <EmptyPanel
                body="Choose a session to expose the segment ladder. Each card keeps the event count and timing visible without opening the timeline."
                eyebrow="Segments"
                title={selectedSessionId ? "Waiting for segments" : "Pick a session first"}
              />
            )}
          </div>
        </Card>

        <Card className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="eyebrow">Level 3</p>
              <h2 className="text-2xl font-semibold">Event timeline</h2>
            </div>
            <Badge variant="category">
              {selectedSegmentIndex !== null ? `Segment ${selectedSegmentIndex}` : "No segment"}
            </Badge>
          </div>
          <div className="space-y-3">
            {segmentDetailQuery.data?.events.length ? (
              segmentDetailQuery.data.events.map((event) => (
                <div
                  className={`animate-fade-up rounded-[24px] border p-4 ${eventCardClass(event.event_type)}`}
                  key={event.id}
                  style={{ marginLeft: `${Math.min(event.depth, 6) * 14}px` }}
                >
                  <div className="mb-3 flex flex-wrap items-center gap-2">
                    <Badge className={eventTypeBadgeClass(event.event_type)} variant="category">
                      {humanizeEventType(event.event_type)}
                    </Badge>
                    <Badge variant="category">{`#${event.sequence_number}`}</Badge>
                    {event.sub_agent_id ? (
                      <Badge variant="mono" title={event.sub_agent_id}>
                        {truncateMiddle(event.sub_agent_id, 18)}
                      </Badge>
                    ) : null}
                  </div>
                  <div className="mb-3 flex flex-wrap gap-4 text-xs text-muted-foreground">
                    <span className="inline-flex items-center gap-1">
                      <Clock3 className="h-3.5 w-3.5" />
                      {formatTimeOnly(event.timestamp)}
                    </span>
                    <span className="numeric">Tokens {formatInteger(event.token_count)}</span>
                    <span className="numeric">Duration {event.duration_ms ?? 0}ms</span>
                    <span className="numeric">Depth {event.depth}</span>
                  </div>
                  {event.data != null &&
                    typeof event.data === "object" &&
                    Object.keys(event.data).length > 0 && (
                      <pre className="overflow-x-auto rounded-[20px] bg-slate-950 p-4 font-mono text-xs leading-6 text-slate-100">
                        {JSON.stringify(event.data, null, 2)}
                      </pre>
                    )}
                </div>
              ))
            ) : (
              <EmptyPanel
                body="The selected segment expands into the exact event sequence here, including depth indentation for nested sub-agent work."
                eyebrow="Timeline"
                title={selectedSegmentIndex !== null ? "Waiting for events" : "Pick a segment first"}
              />
            )}
          </div>
          {segmentDetailQuery.data?.events.length ? (
            <div className="rounded-[24px] border border-dashed border-border/80 bg-white/50 p-4">
              <div className="mb-2 flex items-center gap-2 font-medium text-foreground">
                <Sparkles className="h-4 w-4 text-accent" />
                Spatial truth
              </div>
              <p className="text-sm leading-6 text-muted-foreground">
                Nested events are indented by sub-agent depth so handoffs read like a single
                timeline instead of flat log noise.
              </p>
            </div>
          ) : null}
        </Card>
      </div>
    </section>
  );
}
