import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type SessionSummary = {
  session_id: string;
  project_id: string;
  first_event_at: string;
  last_event_at: string;
  event_count: number;
  segment_count: number;
};

export type ListSessionsResponse = {
  sessions: SessionSummary[];
  next_cursor: string | null;
};

export type SegmentSummary = {
  session_id: string;
  segment_index: number;
  first_event_at: string;
  last_event_at: string;
  event_count: number;
  duration_ms: number | null;
  token_count: number | null;
};

export type ListSegmentsResponse = {
  segments: SegmentSummary[];
};

export type EventRecord = {
  id: string;
  session_id: string;
  project_id: string;
  event_type: string;
  timestamp: string;
  sequence_number: number;
  segment_index: number;
  sub_agent_id: string | null;
  depth: number;
  duration_ms: number | null;
  token_count: number | null;
  data: Record<string, unknown>;
  schema_version: string;
};

export type SegmentDetail = {
  session_id: string;
  segment_index: number;
  events: EventRecord[];
};

export type AnalysisSummary = {
  project_id: string;
  analyzed_session_count: number;
  flag_counts_by_type: Record<string, number>;
  active_directive_count: number;
  last_analyzed_at: string | null;
  as_of: string;
};

export type SessionAnalysisItem = {
  session_id: string;
  analyzed_at: string;
  headline: string;
  flag_count: number;
  key_findings: string[];
};

export type SessionAnalysisList = {
  project_id: string;
  items: SessionAnalysisItem[];
  limit: number;
  offset: number;
  next_offset: number | null;
};

export type BehaviorFlag = {
  id: string;
  project_id: string;
  session_id: string;
  segment_index: number;
  flag_type: string;
  event_ids: string[];
  intent_summary: string;
  reason: string;
  confidence: "high" | "medium" | "low";
  created_at: string;
};

export type SessionAnalysisDetail = {
  project_id: string;
  session_id: string;
  headline: string;
  body: string;
  key_findings: string[];
  analyzed_at: string;
  flags: BehaviorFlag[];
};

export type TrendsBucket = {
  session_id: string;
  analyzed_at: string;
  counts_by_type: Record<string, number>;
};

export type TrendsResponse = {
  project_id: string;
  buckets: TrendsBucket[];
};

export type AggregationResponse = {
  project_id: string;
  flag_counts_by_type: Record<string, number>;
  session_counts_by_type: Record<string, number>;
};

export type Directive = {
  id: string;
  project_id: string;
  type: string;
  status: string;
  instruction: string;
  frequency: number | null;
  trigger_pattern: string | null;
  confidence: number | null;
  max_firing: number | null;
  source_flag_type: string | null;
  source_sessions: string[];
  identity_key: string;
  created_at: string;
  expires_at: string | null;
  updated_at: string;
  disabled_at: string | null;
  disabled_reason: string | null;
};

type DirectivePatch = {
  status: "active" | "disabled";
  reason?: string;
};

const etagCache = new Map<string, string>();
const payloadCache = new Map<string, unknown>();

function buildUrl(path: string, params: Record<string, string | number | boolean | undefined>) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) {
      continue;
    }
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

async function readJson<T>(
  cacheKey: string,
  path: string,
  params: Record<string, string | number | boolean | undefined>,
): Promise<T> {
  const headers: HeadersInit = {};
  const etag = etagCache.get(cacheKey);
  if (etag) {
    headers["If-None-Match"] = etag;
  }

  const response = await fetch(buildUrl(path, params), { headers });
  if (response.status === 304) {
    const cached = payloadCache.get(cacheKey);
    if (cached) {
      return cached as T;
    }
  }

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed: ${response.status}`);
  }

  const nextEtag = response.headers.get("etag");
  if (nextEtag) {
    etagCache.set(cacheKey, nextEtag);
  }

  const payload = (await response.json()) as T;
  payloadCache.set(cacheKey, payload);
  return payload;
}

async function patchDirective(
  projectId: string,
  directiveId: string,
  patch: DirectivePatch,
): Promise<Directive> {
  const response = await fetch(
    buildUrl(`/api/directives/${directiveId}`, { project_id: projectId }),
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(patch),
    },
  );

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `PATCH failed: ${response.status}`);
  }

  const payload = (await response.json()) as Directive;
  const summaryKey = `analysis-summary:${projectId}`;
  etagCache.delete(summaryKey);
  payloadCache.delete(summaryKey);
  etagCache.delete(`directives:${projectId}:all`);
  payloadCache.delete(`directives:${projectId}:all`);
  etagCache.delete(`directives:${projectId}:active`);
  payloadCache.delete(`directives:${projectId}:active`);
  return payload;
}

export function useObservationSessions(projectId: string) {
  return useQuery({
    queryKey: ["observation-sessions", projectId],
    queryFn: () =>
      readJson<ListSessionsResponse>(`observation-sessions:${projectId}`, "/api/sessions", {
        project_id: projectId,
        limit: 50,
      }),
  });
}

export function useSegments(projectId: string, sessionId: string | null) {
  return useQuery({
    queryKey: ["observation-segments", projectId, sessionId],
    enabled: Boolean(projectId && sessionId),
    queryFn: () =>
      readJson<ListSegmentsResponse>(
        `observation-segments:${projectId}:${sessionId}`,
        `/api/sessions/${sessionId}/segments`,
        { project_id: projectId },
      ),
  });
}

export function useSegmentDetail(
  projectId: string,
  sessionId: string | null,
  segmentIndex: number | null,
) {
  return useQuery({
    queryKey: ["observation-segment-detail", projectId, sessionId, segmentIndex],
    enabled: Boolean(projectId && sessionId && segmentIndex !== null),
    queryFn: () =>
      readJson<SegmentDetail>(
        `observation-segment:${projectId}:${sessionId}:${segmentIndex}`,
        `/api/sessions/${sessionId}/segments/${segmentIndex}`,
        { project_id: projectId },
      ),
  });
}

export function useAnalysisSummary(projectId: string) {
  return useQuery({
    queryKey: ["analysis-summary", projectId],
    queryFn: () =>
      readJson<AnalysisSummary>(`analysis-summary:${projectId}`, "/api/analysis/summary", {
        project_id: projectId,
      }),
  });
}

export function useAnalysisSessions(projectId: string) {
  return useQuery({
    queryKey: ["analysis-sessions", projectId],
    queryFn: () =>
      readJson<SessionAnalysisList>(`analysis-sessions:${projectId}`, "/api/analysis/sessions", {
        project_id: projectId,
        limit: 50,
      }),
  });
}

export function useAnalysisDetail(projectId: string, sessionId: string | null) {
  return useQuery({
    queryKey: ["analysis-detail", projectId, sessionId],
    enabled: Boolean(projectId && sessionId),
    queryFn: () =>
      readJson<SessionAnalysisDetail>(
        `analysis-detail:${projectId}:${sessionId}`,
        `/api/analysis/sessions/${sessionId}`,
        { project_id: projectId },
      ),
  });
}

export function useAnalysisTrends(projectId: string) {
  return useQuery({
    queryKey: ["analysis-trends", projectId],
    queryFn: () =>
      readJson<TrendsResponse>(`analysis-trends:${projectId}`, "/api/analysis/trends", {
        project_id: projectId,
        limit: 24,
      }),
  });
}

export function useAnalysisAggregation(projectId: string) {
  return useQuery({
    queryKey: ["analysis-aggregation", projectId],
    queryFn: () =>
      readJson<AggregationResponse>(
        `analysis-aggregation:${projectId}`,
        "/api/analysis/aggregation",
        {
          project_id: projectId,
        },
      ),
  });
}

export function useDirectives(projectId: string, activeOnly: boolean) {
  return useQuery({
    queryKey: ["directives", projectId, activeOnly],
    queryFn: () =>
      readJson<Directive[]>(
        `directives:${projectId}:${activeOnly ? "active" : "all"}`,
        "/api/directives",
        {
          project_id: projectId,
          active: activeOnly,
        },
      ),
  });
}

export function useDirectiveMutation(projectId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      directiveId,
      patch,
    }: {
      directiveId: string;
      patch: DirectivePatch;
    }) => patchDirective(projectId, directiveId, patch),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["directives", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["analysis-summary", projectId] }),
      ]);
    },
  });
}
