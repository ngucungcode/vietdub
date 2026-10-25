"use client";

import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type JsonObject = Record<string, unknown>;

type GpuDevice = {
  uuid?: string;
  name?: string;
  driver_version?: string;
  memory_total_mib?: number;
  compute_capability?: string;
};

type GpuSupportTier = "supported" | "maintenance-limited" | "experimental";

type Health = {
  status: "ok" | "degraded" | "error";
  api_version?: string;
  compute_mode?: "cpu" | "gpu";
  database?: { status?: string; journal_mode?: string };
  model_catalog?: { count?: number; status?: string };
  acquisition_configured?: boolean;
  coordinator_configured?: boolean;
  gpu?: {
    ready?: boolean;
    enforced?: boolean;
    minimum_driver?: string;
    minimum_compute_capability?: string;
    minimum_vram_mib?: number;
    support_tier?: GpuSupportTier | null;
    gpus?: GpuDevice[];
    errors?: string[];
    warnings?: string[];
  };
};



type ModelEntry = {
  id: string;
  stage: string;
  backend: string;
  license: string;
  languages?: string[];
  minimum_vram_mib?: number | null;
  installed?: boolean;
  valid?: boolean;
  selectable?: boolean;
  quality_tier?: string;
  compute_type?: string;
};

type ModelCatalog = {
  schema_version: number;
  models: ModelEntry[];
};

type JobError = {
  code?: string;
  message?: string;
  retryable?: boolean;
};

type JobDetails = JsonObject & {
  transcript_source?: unknown;
  stage_progress_permille?: unknown;
  downloaded_bytes?: unknown;
  total_bytes?: unknown;
  speed_bytes_per_second?: unknown;
  eta_seconds?: unknown;
  asr_step?: unknown;
  asr_processed_us?: unknown;
  asr_duration_us?: unknown;
  asr_segment_count?: unknown;
  asr_progress_permille?: unknown;
  translation_completed_blocks?: unknown;
  translation_block_count?: unknown;
  separation_progress_permille?: unknown;
  phase4_message?: unknown;
  tts_completed_blocks?: unknown;
  tts_block_count?: unknown;
  timing_completed_blocks?: unknown;
  timing_block_count?: unknown;
  export_processed_us?: unknown;
  export_duration_us?: unknown;
};

type Job = {
  id: string;
  release_id: string;
  status: string;
  stage: string;
  progress_permille: number;
  spec?: {
    search_query?: string;
    source_language?: string;
    subtitle_mode?: string;
    timing_profile?: "natural" | "strict";
    glossary?: Record<string, string>;
    models?: Record<string, string | null>;
    voice?: { voice_id?: string | null; reference_path?: string | null } | null;
    translation_backend?: string;
    translation_model?: string | null;
    require_final_approval?: boolean;
    speaker_voices?: Record<string, { voice_id?: string | null; reference_path?: string | null }>;
    diarize_speakers?: boolean;
    auto_speaker_voices?: boolean;
    qc_enabled?: boolean;
  };
  details?: JobDetails;
  error?: JobError | null;
  result?: JsonObject | null;
  cancel_requested?: boolean;
  created_at: string;
  updated_at: string;
};

type StudioCastMember = {
  speaker: string;
  display_name?: string | null;
  line_count?: number;
  first_start_us?: number;
  voice_id?: string | null;
  reference_path?: string | null;
};

type StudioScriptLine = {
  ordinal: number;
  start_us: number;
  end_us: number;
  speaker?: string | null;
  display_name?: string | null;
  text: string;
  original_text?: string | null;
  voice_id?: string | null;
  synthesized?: boolean;
  qc_status?: string | null;
  qc_heard?: string | null;
  qc_wer?: number | null;
};

type StudioQcSummary = {
  total?: number;
  ok?: number;
  minor?: number;
  review?: number;
  bad?: number;
  silent?: number;
  needs_attention?: number;
  mean_wer?: number;
};

type StudioMix = {
  narration_lufs?: number | null;
  accompaniment_lufs?: number | null;
  ducking_ratio?: number | null;
  ducking_threshold?: number | null;
  true_peak_db?: number | null;
};

type StudioView = {
  job_id: string;
  status: string;
  editable: boolean;
  cast: StudioCastMember[];
  script: StudioScriptLine[];
  mix: StudioMix;
  tts_backend: string;
  qc_summary?: StudioQcSummary | null;
};

type ReviewView = StudioView & {
  preview_video_url?: string | null;
  preview_subtitle_url?: string | null;
};

type StudioCastResponse = (StudioView & { requeued?: boolean }) | { requeued?: boolean };

type StudioCastDraft = {
  display_name: string;
  voice_id: string;
  reference_path: string;
};

type StudioMixDraft = {
  narration_lufs: string;
  accompaniment_lufs: string;
  ducking_ratio: string;
  ducking_threshold: string;
  true_peak_db: string;
};

type SubtitleCandidate = {
  subtitle_id: string;
  source?: string;
  language?: string;
  format?: string;
  score?: number;
  high_confidence?: boolean;
  release_name?: string;
  fps?: number | null;
  hearing_impaired?: boolean;
  forced?: boolean;
  matched_by?: string[] | string;
};

type LanguageCandidate = {
  language?: string;
  probability?: number;
  confidence?: number;
};

type IntegrationComponent = {
  configured?: boolean;
  editable?: boolean;
  can_manage?: boolean;
  cleanup_pending?: boolean;
  can_delete?: boolean;
};

type IntegrationsStatus = {
  opensubtitles?: IntegrationComponent;
};

type UploadSession = {
  id: string;
  status: string;
  media_filename: string;
  subtitle_filename?: string | null;
  media_size_bytes?: number | null;
  subtitle_size_bytes?: number | null;
  media_sha256?: string | null;
  subtitle_sha256?: string | null;
  job_id?: string | null;
};

type UploadPhase = "preparing" | "media" | "subtitle" | "finalizing" | "cancelling";

type UploadProgress = {
  phase: UploadPhase;
  filename?: string;
  fileLoaded: number;
  fileTotal: number;
  overallLoaded: number;
  overallTotal: number;
  speedBytesPerSecond: number;
};

const uploadPhaseLabels: Record<UploadPhase, string> = {
  preparing: "Đang tạo phiên tải",
  media: "Đang tải video",
  subtitle: "Đang tải phụ đề",
  finalizing: "Đang kiểm tra và tạo job",
  cancelling: "Đang hủy và dọn dữ liệu tạm",
};

const stageOrder = [
  "acquisition",
  "subtitle",
  "asr",
  "translation",
  "separation",
  "tts",
  "timing",
  "mix",
  "export",
  "verify",
  "done",
];

const stageLabels: Record<string, string> = {
  acquisition: "Lấy nguồn",
  subtitle: "Phụ đề",
  asr: "Nhận dạng",
  translation: "Dịch lời",
  separation: "Tách thoại",
  tts: "Tạo giọng",
  timing: "Khớp nhịp",
  mix: "Phối âm",
  export: "Dựng MP4",
  verify: "Kiểm tra",
  done: "Hoàn tất",
  mt: "Dịch lời",
  "tts-support": "Hỗ trợ tạo giọng",
};

const statusLabels: Record<string, string> = {
  created: "Đã tạo",
  searching: "Đang tìm nguồn",
  awaiting_release_selection: "Chờ chọn nguồn",
  downloading: "Đang tải",
  subtitle_matching: "Đang khớp phụ đề",
  ready_offline: "Sẵn sàng xử lý offline",
  transcribing: "Đang nhận dạng",
  subtitle_selected: "Đã chọn phụ đề",
  ready_translation: "Sẵn sàng dịch",
  translating: "Đang dịch",
  ready_tts: "Sẵn sàng tạo giọng",
  separating: "Đang tách thoại",
  synthesizing: "Đang tạo giọng",
  timing: "Đang khớp thời lượng",
  mixing: "Đang phối âm",
  muxing: "Đang dựng MP4",
  verifying: "Đang kiểm tra đầu ra",
  completed: "Hoàn tất",
  awaiting_approval: "Chờ duyệt kết quả",
  paused: "Tạm dừng",
  failed: "Có lỗi",
  cancelling: "Đang hủy",
  cancelled: "Đã hủy",
  needs_language: "Cần chọn ngôn ngữ",
  needs_subtitle_selection: "Cần chọn phụ đề",
};

const languageOptions = [
  ["auto", "Tự động nhận diện"],
  ["en", "Tiếng Anh"],
  ["ja", "Tiếng Nhật"],
  ["ko", "Tiếng Hàn"],
  ["th", "Tiếng Thái"],
  ["ar", "Tiếng Ả Rập"],
  ["zh", "Tiếng Trung"],
  ["fr", "Tiếng Pháp"],
  ["de", "Tiếng Đức"],
  ["es", "Tiếng Tây Ban Nha"],
  ["vi", "Tiếng Việt"],
] as const;

const modelStages = [
  ["asr", "Nhận dạng lời nói"],
  ["mt", "Dịch sang tiếng Việt"],
  ["separation", "Tách thoại / nhạc nền"],
  ["tts", "Giọng thuyết minh"],
] as const;

const nonActiveStatuses = new Set(["completed", "failed", "cancelled", "paused"]);

const terminalStatuses = new Set(["completed", "failed", "cancelled"]);

const stageProgressRanges: Record<string, readonly [number, number]> = {
  acquisition: [0, 200],
  subtitle: [200, 250],
  asr: [275, 450],
  translation: [450, 650],
  separation: [650, 735],
  tts: [735, 850],
  timing: [850, 900],
  mix: [900, 910],
  export: [910, 985],
  verify: [985, 1000],
  done: [1000, 1000],
};

type ProgressMetric = {
  label: string;
  value: string;
  hint?: string;
};

type JobProgressView = {
  overallPercent: number;
  stagePercent: number;
  summary: string;
  metrics: ProgressMetric[];
};

class ApiRequestError extends Error {
  code?: string;
  retryable?: boolean;

  constructor(message: string, code?: string, retryable?: boolean) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.retryable = retryable;
  }
}

class UploadCancelledError extends Error {
  constructor() {
    super("Đã hủy tải tệp.");
    this.name = "UploadCancelledError";
  }
}

async function api<T>(path: string, init?: RequestInit, admin = false): Promise<T> {
  const response = await fetch(`/v1${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      ...(init?.body ? { "content-type": "application/json" } : {}),
      ...(admin ? { "X-Dub-Admin-Request": "1" } : {}),
      ...init?.headers,
    },
  });
  const payload = (await response.json().catch(() => null)) as
    | { detail?: string | { code?: string; message?: string; retryable?: boolean }; message?: string }
    | null;
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : detail?.message ?? payload?.message;
    throw new ApiRequestError(
      message || `Yêu cầu thất bại (${response.status})`,
      typeof detail === "object" ? detail?.code : undefined,
      typeof detail === "object" ? detail?.retryable : undefined,
    );
  }
  return payload as T;
}

function fileExtension(filename: string) {
  const separator = filename.lastIndexOf(".");
  return separator < 0 ? "" : filename.slice(separator).toLowerCase();
}

function parseGlossary(value: string): Record<string, string> {
  const glossary: Record<string, string> = {};
  for (const [index, rawLine] of value.split(/\r?\n/).entries()) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    const source = separator >= 0 ? line.slice(0, separator).trim() : "";
    const target = separator >= 0 ? line.slice(separator + 1).trim() : "";
    if (!source || !target) throw new Error(`Glossary dòng ${index + 1} phải có dạng nguồn = tiếng Việt.`);
    if (source in glossary) throw new Error(`Glossary bị trùng thuật ngữ: ${source}`);
    glossary[source] = target;
  }
  if (Object.keys(glossary).length > 100) throw new Error("Glossary không được vượt quá 100 thuật ngữ.");
  return glossary;
}

function parseSpeakerVoices(value: string): Record<string, { voice_id: string | null; reference_path: string | null }> {
  const voices: Record<string, { voice_id: string | null; reference_path: string | null }> = {};
  for (const [index, rawLine] of value.split(/\r?\n/).entries()) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    const label = separator >= 0 ? line.slice(0, separator).trim() : "";
    const target = separator >= 0 ? line.slice(separator + 1).trim() : "";
    if (!label || !target) throw new Error(`Giọng theo nhân vật dòng ${index + 1} phải có dạng NHÃN=voice_id hoặc NHÃN=đường dẫn file.`);
    if (label in voices) throw new Error(`Giọng theo nhân vật bị trùng nhãn: ${label}`);
    const isFile = /\.(wav|mp3|flac|ogg)$/i.test(target) || target.includes("/") || target.includes("\\");
    voices[label] = isFile
      ? { voice_id: null, reference_path: target }
      : { voice_id: target, reference_path: null };
  }
  return voices;
}

function uploadRequestError(xhr: XMLHttpRequest) {
  let message: string | undefined;
  let code: string | undefined;
  let retryable: boolean | undefined;
  try {
    const payload = JSON.parse(xhr.responseText) as {
      detail?: string | { code?: string; message?: string; retryable?: boolean };
      message?: string;
    };
    if (typeof payload.detail === "string") {
      message = payload.detail;
    } else if (payload.detail) {
      message = payload.detail.message;
      code = payload.detail.code;
      retryable = payload.detail.retryable;
    }
    message ??= payload.message;
  } catch {
    // The proxy may return an empty or non-JSON error body.
  }
  return new ApiRequestError(
    message || `Không thể tải tệp lên (${xhr.status || "mất kết nối"}).`,
    code,
    retryable,
  );
}

function uploadBinary(
  path: string,
  file: File,
  phase: "media" | "subtitle",
  baseLoaded: number,
  overallTotal: number,
  requestRef: { current: XMLHttpRequest | null },
  onProgress: (progress: UploadProgress) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const startedAt = performance.now();
    requestRef.current = xhr;
    xhr.open("PUT", path);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      const loaded = Math.min(event.loaded, file.size);
      const elapsedSeconds = Math.max((performance.now() - startedAt) / 1000, 0.001);
      onProgress({
        phase,
        filename: file.name,
        fileLoaded: loaded,
        fileTotal: file.size,
        overallLoaded: baseLoaded + loaded,
        overallTotal,
        speedBytesPerSecond: loaded / elapsedSeconds,
      });
    };
    xhr.onload = () => {
      requestRef.current = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress({
          phase,
          filename: file.name,
          fileLoaded: file.size,
          fileTotal: file.size,
          overallLoaded: baseLoaded + file.size,
          overallTotal,
          speedBytesPerSecond: file.size / Math.max((performance.now() - startedAt) / 1000, 0.001),
        });
        resolve();
      } else {
        reject(uploadRequestError(xhr));
      }
    };
    xhr.onerror = () => {
      requestRef.current = null;
      reject(new ApiRequestError(
        "Mất kết nối trong khi tải tệp lên máy xử lý.",
        "upload_network_error",
        true,
      ));
    };
    xhr.onabort = () => {
      requestRef.current = null;
      reject(new UploadCancelledError());
    };
    xhr.send(file);
  });
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function nonNegativeNumber(value: unknown): number | null {
  const parsed = finiteNumber(value);
  return parsed === null || parsed < 0 ? null : parsed;
}

function clampPercent(value: number) {
  return Math.max(0, Math.min(100, value));
}

function ratioPercent(completed: unknown, total: unknown): number | null {
  const completedValue = nonNegativeNumber(completed);
  const totalValue = nonNegativeNumber(total);
  if (completedValue === null || totalValue === null || totalValue <= 0) return null;
  return clampPercent((completedValue / totalValue) * 100);
}

function formatBytes(value?: unknown) {
  const parsed = nonNegativeNumber(value);
  if (parsed === null) return "Chưa rõ dung lượng";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = parsed;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function formatGpuMemory(value: unknown) {
  const memoryMib = nonNegativeNumber(value);
  if (memoryMib === null) return "VRAM chưa rõ";
  const memoryGib = memoryMib / 1024;
  return `${memoryGib.toLocaleString("vi-VN", {
    minimumFractionDigits: Number.isInteger(memoryGib) ? 0 : 1,
    maximumFractionDigits: 1,
  })} GiB VRAM`;
}

function gpuWarningMessage(value: string) {
  const warning = value.trim();
  if (warning.includes("maintenance-limited Volta sm_70")) {
    return "Volta sm_70 đang ở mức hỗ trợ bảo trì giới hạn.";
  }
  if (warning.includes("experimental GPU support tier")) {
    return "GPU đang ở mức hỗ trợ thử nghiệm; chỉ dùng profile minimal và cần nghiệm thu trên card thật.";
  }
  return warning
    .replace(/^logical CUDA device 0:/, "GPU CUDA logical 0:")
    .replace(/^non-selected NVIDIA device ([0-9]+):/, "GPU NVIDIA không được chọn $1:")
    .replace(
      /compute capability ([^ ]+) is not in the supported CUDA architecture matrix/g,
      "compute capability $1 không thuộc ma trận kiến trúc CUDA được hỗ trợ",
    )
    .replace(/driver ([^ ]+) < ([^,]+)/g, "driver $1 thấp hơn $2")
    .replace(/VRAM ([0-9]+ MiB) < ([0-9]+ MiB)/g, "VRAM $1 thấp hơn $2");
}

function formatByteRate(value: unknown) {
  const parsed = nonNegativeNumber(value);
  return parsed === null ? "Chưa đo được" : `${formatBytes(parsed)}/giây`;
}

function formatDurationSeconds(value: unknown) {
  const parsed = nonNegativeNumber(value);
  if (parsed === null) return "Chưa ước tính";
  const totalSeconds = Math.round(parsed);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours} giờ ${String(minutes).padStart(2, "0")} phút`;
  if (minutes > 0) return `${minutes} phút ${String(seconds).padStart(2, "0")} giây`;
  return `${seconds} giây`;
}

function formatDurationUs(value: unknown) {
  const parsed = nonNegativeNumber(value);
  if (parsed === null) return "Chưa rõ";
  const totalSeconds = Math.round(parsed / 1_000_000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatMmSs(value: unknown) {
  const parsed = nonNegativeNumber(value);
  if (parsed === null) return "??:??";
  const totalSeconds = Math.floor(parsed / 1_000_000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatCount(value: unknown) {
  const parsed = nonNegativeNumber(value);
  return parsed === null ? "—" : Math.round(parsed).toLocaleString("vi-VN");
}

function formatElapsed(createdAt: string, updatedAt: string, status: string, nowMs: number) {
  const start = new Date(createdAt).valueOf();
  if (!Number.isFinite(start) || nowMs <= 0) return "Đang tính…";
  const terminalEnd = new Date(updatedAt).valueOf();
  const end = terminalStatuses.has(status) && Number.isFinite(terminalEnd) ? terminalEnd : nowMs;
  return formatDurationSeconds(Math.max(0, (end - start) / 1000));
}

function formatFreshness(updatedAt: string, nowMs: number) {
  const updated = new Date(updatedAt).valueOf();
  if (!Number.isFinite(updated) || nowMs <= 0) return "Đang đồng bộ…";
  const seconds = Math.max(0, Math.floor((nowMs - updated) / 1000));
  if (seconds < 5) return "Vừa cập nhật";
  if (seconds < 60) return `${seconds} giây trước`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`;
  return formatDate(updatedAt);
}

function formatDate(value?: string | null) {
  if (!value) return "Không rõ ngày";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("vi-VN");
}

function shortId(value: string) {
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value;
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function warningMessages(...values: unknown[]): string[] {
  const messages: string[] = [];
  for (const value of values) {
    for (const item of asArray<unknown>(value)) {
      if (typeof item === "string" && item.trim()) {
        messages.push(item.trim());
      } else if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        const message = typeof record.message === "string" ? record.message.trim() : "";
        const code = typeof record.code === "string" ? record.code.trim() : "";
        if (message) messages.push(code ? `${code}: ${message}` : message);
      }
    }
  }
  return [...new Set(messages)];
}

function messageOf(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

const STUDIO_REQUEUE_NOTICE =
  "Đã đưa job về hàng chờ chạy lại Phase 4 (chỉ thu lại phần thay đổi, các câu khác giữ nguyên, trừ khi đổi giọng thì thu lại toàn bộ)";

function stagePosition(job: Job) {
  if (job.status === "completed") return stageOrder.length - 1;
  const index = stageOrder.indexOf(job.stage);
  return index < 0 ? 0 : index;
}

function isSkippedTranscriptStage(job: Job, stage: string) {
  const source = job.details?.transcript_source;
  return (source === "asr" && stage === "subtitle") || (source === "subtitle" && stage === "asr");
}

function asrStepLabel(value: unknown) {
  if (value === "preparing") return "Đang chuẩn bị model";
  if (value === "decoding") return "Đang giải mã âm thanh";
  if (value === "recognizing") return "Đang nhận dạng lời nói";
  if (value === "finalizing") return "Đang hoàn thiện transcript";
  return "Đang xử lý nhận dạng";
}

function fallbackStagePercent(job: Job) {
  if (job.status === "completed" || job.stage === "done") return 100;
  const range = stageProgressRanges[job.stage];
  if (!range) return 0;
  const [start, end] = range;
  if (end <= start) return 100;
  return clampPercent(((job.progress_permille - start) / (end - start)) * 100);
}

function jobProgressView(job: Job): JobProgressView {
  const details = job.details ?? {};
  const overallPercent = clampPercent((finiteNumber(job.progress_permille) ?? 0) / 10);
  let stagePercent: number | null = null;
  let summary = statusLabels[job.status] ?? job.status;
  const metrics: ProgressMetric[] = [];

  if (job.stage === "acquisition") {
    stagePercent = nonNegativeNumber(details.stage_progress_permille);
    stagePercent = stagePercent === null ? ratioPercent(details.downloaded_bytes, details.total_bytes) : stagePercent / 10;
    const downloaded = nonNegativeNumber(details.downloaded_bytes);
    const total = nonNegativeNumber(details.total_bytes);
    const speed = nonNegativeNumber(details.speed_bytes_per_second);
    const eta = nonNegativeNumber(details.eta_seconds);
    if (downloaded !== null || total !== null) {
      const transferred = `${formatBytes(downloaded)}${total !== null ? ` / ${formatBytes(total)}` : ""}`;
      metrics.push({ label: "Đã tải", value: transferred });
      summary = transferred;
    }
    if (speed !== null) metrics.push({ label: "Tốc độ", value: formatByteRate(speed) });
    if (eta !== null) metrics.push({ label: "Còn lại", value: formatDurationSeconds(eta), hint: "ước tính" });
  } else if (job.stage === "asr") {
    stagePercent = nonNegativeNumber(details.asr_progress_permille);
    stagePercent = stagePercent === null
      ? ratioPercent(details.asr_processed_us, details.asr_duration_us)
      : stagePercent / 10;
    const processed = nonNegativeNumber(details.asr_processed_us);
    const duration = nonNegativeNumber(details.asr_duration_us);
    const segments = nonNegativeNumber(details.asr_segment_count);
    if (processed !== null || duration !== null) {
      const timeline = `${formatDurationUs(processed)}${duration !== null ? ` / ${formatDurationUs(duration)}` : ""}`;
      metrics.push({ label: "Đã nhận dạng", value: timeline });
      summary = timeline;
    }
    if (segments !== null) metrics.push({ label: "Đoạn lời", value: formatCount(segments), hint: "đã nhận diện" });
    if (typeof details.asr_step === "string" && details.asr_step) {
      metrics.push({
        label: "Tác vụ ASR",
        value: asrStepLabel(details.asr_step),
      });
    }
  } else if (job.stage === "translation") {
    stagePercent = ratioPercent(details.translation_completed_blocks, details.translation_block_count);
    const completed = nonNegativeNumber(details.translation_completed_blocks);
    const total = nonNegativeNumber(details.translation_block_count);
    if (completed !== null || total !== null) {
      const blocks = `${formatCount(completed)}${total !== null ? ` / ${formatCount(total)}` : ""}`;
      metrics.push({ label: "Block đã dịch", value: blocks });
      summary = `${blocks} block`;
    }
  } else if (job.stage === "separation") {
    const permille = nonNegativeNumber(details.separation_progress_permille);
    stagePercent = permille === null ? null : permille / 10;
    if (typeof details.phase4_message === "string" && details.phase4_message.trim()) {
      summary = details.phase4_message.trim();
      metrics.push({ label: "Đang thực hiện", value: summary });
    }
  } else if (job.stage === "tts") {
    stagePercent = ratioPercent(details.tts_completed_blocks, details.tts_block_count);
    const completed = nonNegativeNumber(details.tts_completed_blocks);
    const total = nonNegativeNumber(details.tts_block_count);
    if (completed !== null || total !== null) {
      const blocks = `${formatCount(completed)}${total !== null ? ` / ${formatCount(total)}` : ""}`;
      metrics.push({ label: "Block tạo giọng", value: blocks });
      summary = `${blocks} block`;
    }
  } else if (job.stage === "timing") {
    stagePercent = ratioPercent(details.timing_completed_blocks, details.timing_block_count);
    const completed = nonNegativeNumber(details.timing_completed_blocks);
    const total = nonNegativeNumber(details.timing_block_count);
    if (completed !== null || total !== null) {
      const blocks = `${formatCount(completed)}${total !== null ? ` / ${formatCount(total)}` : ""}`;
      metrics.push({ label: "Block đã khớp", value: blocks });
      summary = `${blocks} block`;
    }
    if (
      details.phase4_step === "timing_rewrite" &&
      typeof details.phase4_message === "string" &&
      details.phase4_message.trim()
    ) {
      summary = details.phase4_message.trim();
      metrics.push({ label: "Đang xử lý", value: summary });
      const attempt = nonNegativeNumber(details.timing_rewrite_attempt);
      if (attempt !== null) metrics.push({ label: "Lần rút gọn", value: formatCount(attempt) });
    }
  } else if (job.stage === "export") {
    stagePercent = ratioPercent(details.export_processed_us, details.export_duration_us);
    const processed = nonNegativeNumber(details.export_processed_us);
    const duration = nonNegativeNumber(details.export_duration_us);
    if (processed !== null || duration !== null) {
      const timeline = `${formatDurationUs(processed)}${duration !== null ? ` / ${formatDurationUs(duration)}` : ""}`;
      metrics.push({ label: "Đã dựng", value: timeline });
      summary = timeline;
    }
  }

  const normalizedStagePercent = clampPercent(stagePercent ?? fallbackStagePercent(job));
  metrics.unshift({
    label: "Công đoạn hiện tại",
    value: `${normalizedStagePercent.toFixed(1)}%`,
    hint: stageLabels[job.stage] ?? job.stage,
  });
  return { overallPercent, stagePercent: normalizedStagePercent, summary, metrics };
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);

  const [catalog, setCatalog] = useState<ModelCatalog>({ schema_version: 1, models: [] });
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [mediaFile, setMediaFile] = useState<File | null>(null);
  const [subtitleFile, setSubtitleFile] = useState<File | null>(null);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [uploadSessionId, setUploadSessionId] = useState<string | null>(null);
  const [sourceLanguage, setSourceLanguage] = useState("auto");

  const [timingProfile, setTimingProfile] = useState<"natural" | "strict">("natural");
  const [selectedModels, setSelectedModels] = useState<Record<string, string>>({});
  const [voiceId, setVoiceId] = useState("");
  const [voiceReferencePath, setVoiceReferencePath] = useState("");
  const [voiceRightsConfirmed, setVoiceRightsConfirmed] = useState(false);
  const [glossaryText, setGlossaryText] = useState("");
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [languageChoice, setLanguageChoice] = useState("en");
  const [submitting, setSubmitting] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(0);
  const [progressStream, setProgressStream] = useState<{
    jobId: string;
    state: "live" | "fallback";
  } | null>(null);

  const [integrations, setIntegrations] = useState<IntegrationsStatus | null>(null);
  const [integrationBusy, setIntegrationBusy] = useState<string | null>(null);
  const [integrationNotice, setIntegrationNotice] = useState<string | null>(null);
  const [integrationProblem, setIntegrationProblem] = useState<string | null>(null);
  const [openSubApiKey, setOpenSubApiKey] = useState("");
  const [openSubUsername, setOpenSubUsername] = useState("");
  const [openSubPassword, setOpenSubPassword] = useState("");
  const [studio, setStudio] = useState<StudioView | null>(null);
  const [studioError, setStudioError] = useState<string | null>(null);
  const [studioNotice, setStudioNotice] = useState<string | null>(null);
  const [studioBusy, setStudioBusy] = useState<string | null>(null);
  const [castDraft, setCastDraft] = useState<Record<string, StudioCastDraft>>({});
  const [scriptDraft, setScriptDraft] = useState<Record<number, string>>({});
  const [mixDraft, setMixDraft] = useState<StudioMixDraft>({
    narration_lufs: "",
    accompaniment_lufs: "",
    ducking_ratio: "",
    ducking_threshold: "",
    true_peak_db: "",
  });
  const [translationBackend, setTranslationBackend] = useState<"local" | "openai">("local");
  const [openaiModel, setOpenaiModel] = useState("");
  const [requireFinalApproval, setRequireFinalApproval] = useState(true);
  const [speakerVoicesText, setSpeakerVoicesText] = useState("");
  const [diarizeSpeakers, setDiarizeSpeakers] = useState(false);
  const [autoSpeakerVoices, setAutoSpeakerVoices] = useState(false);
  const [qcEnabled, setQcEnabled] = useState(true);
  const [openaiKeyConfigured, setOpenaiKeyConfigured] = useState<boolean | null>(null);
  const [openaiKeyInput, setOpenaiKeyInput] = useState("");
  const [settingsBusy, setSettingsBusy] = useState<string | null>(null);
  const [settingsNotice, setSettingsNotice] = useState<string | null>(null);
  const [settingsProblem, setSettingsProblem] = useState<string | null>(null);
  const [translateKeyMissing, setTranslateKeyMissing] = useState(false);
  const [review, setReview] = useState<ReviewView | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);
  const [rejectNote, setRejectNote] = useState("");
  const uploadRequestRef = useRef<XMLHttpRequest | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const uploadSessionIdRef = useRef<string | null>(null);
  const uploadCancelledRef = useRef(false);
  const uploadFinalizingRef = useRef(false);
  const uploadRequestFingerprintRef = useRef<string | null>(null);
  const uploadedMediaFileRef = useRef<File | null>(null);
  const uploadedSubtitleFileRef = useRef<File | null>(null);
  const mediaInputRef = useRef<HTMLInputElement | null>(null);
  const subtitleInputRef = useRef<HTMLInputElement | null>(null);

  const refreshOverview = useCallback(async () => {
    const [healthResult, jobsResult] = await Promise.allSettled([
      api<Health>("/health"),
      api<{ items: Job[] }>("/jobs?limit=20&newest_first=true"),
    ]);
    setHealth(healthResult.status === "fulfilled" ? healthResult.value : null);
    if (jobsResult.status === "fulfilled") setJobs(jobsResult.value.items);
  }, []);

  const refreshCatalog = useCallback(async () => {
    try {
      setCatalog(await api<ModelCatalog>("/models"));
    } catch {
      setCatalog({ schema_version: 1, models: [] });
    }
  }, []);

  const refreshIntegrations = useCallback(async () => {
    setIntegrationProblem(null);
    try {
      setIntegrations(await api<IntegrationsStatus>("/admin/integrations", undefined, true));
    } catch (error) {
      setIntegrationProblem(messageOf(error, "Không thể tải trạng thái tích hợp."));
    }
  }, []);

  const refreshOpenAiKey = useCallback(async () => {
    try {
      const result = await api<{ configured: boolean }>("/settings/openai-key");
      setOpenaiKeyConfigured(result.configured);
    } catch {
      setOpenaiKeyConfigured(null);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => {
      void refreshOverview();
      void refreshCatalog();
      void refreshIntegrations();
      void refreshOpenAiKey();
    }, 0);
    const timer = window.setInterval(() => void refreshOverview(), 3000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refreshCatalog, refreshIntegrations, refreshOverview, refreshOpenAiKey]);

  useEffect(() => {
    const updateClock = () => setNowMs(Date.now());
    updateClock();
    const timer = window.setInterval(updateClock, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const activeJob = useMemo(
    () => jobs.find((job) => !nonActiveStatuses.has(job.status)),
    [jobs],
  );
  const uploadConfigurationLocked = Boolean(uploadSessionId);
  const selectedJob = useMemo(
    () =>
      jobs.find((job) => job.id === selectedJobId) ??
      jobs.find((job) =>
        ["needs_language", "needs_subtitle_selection", "failed"].includes(job.status),
      ) ??
      jobs[0] ??
      null,
    [jobs, selectedJobId],
  );

  useEffect(() => {
    const jobId = selectedJob?.id;
    if (!jobId || terminalStatuses.has(selectedJob.status)) return;

    const stream = new EventSource(`/v1/jobs/${encodeURIComponent(jobId)}/events`);
    let refreshTimer: number | null = null;
    const requestRefresh = () => {
      if (refreshTimer !== null) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        void refreshOverview();
      }, 150);
    };
    const handleOpen = () => setProgressStream({ jobId, state: "live" });
    const handleError = () => setProgressStream({ jobId, state: "fallback" });
    const eventTypes = [
      "job.created",
      "job.status",
      "job.warning",
      "job.checkpoint",
      "translation.plan",
      "translation.block",
    ];
    stream.addEventListener("open", handleOpen);
    stream.addEventListener("error", handleError);
    for (const eventType of eventTypes) stream.addEventListener(eventType, requestRefresh);

    return () => {
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      stream.removeEventListener("open", handleOpen);
      stream.removeEventListener("error", handleError);
      for (const eventType of eventTypes) stream.removeEventListener(eventType, requestRefresh);
      stream.close();
    };
  }, [refreshOverview, selectedJob?.id, selectedJob?.status]);

  const progressStreamState: "idle" | "connecting" | "live" | "fallback" = !selectedJob || terminalStatuses.has(selectedJob.status)
    ? "idle"
    : progressStream?.jobId === selectedJob.id
      ? progressStream.state
      : "connecting";
  const validModelCount = catalog.models.filter((model) => model.installed && model.valid).length;

  function modelsFor(stage: string) {
    return catalog.models.filter((model) => model.stage === stage && model.selectable !== false);
  }

  function selectMediaFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    setProblem(null);
    setNotice(null);
    if (!file) {
      setMediaFile(null);
      return;
    }
    if (![".mp4", ".mkv"].includes(fileExtension(file.name))) {
      event.target.value = "";
      setMediaFile(null);
      setProblem("Video phải là tệp MP4 hoặc MKV.");
      return;
    }
    if (file.size <= 0) {
      event.target.value = "";
      setMediaFile(null);
      setProblem("Video đang rỗng hoặc trình duyệt không thể đọc tệp.");
      return;
    }
    setMediaFile(file);
  }

  function selectSubtitleFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    setProblem(null);
    setNotice(null);
    if (!file) {
      setSubtitleFile(null);
      return;
    }
    if (fileExtension(file.name) !== ".srt") {
      event.target.value = "";
      setSubtitleFile(null);
      setProblem("Phụ đề thủ công phải là tệp SRT.");
      return;
    }
    if (file.size <= 0) {
      event.target.value = "";
      setSubtitleFile(null);
      setProblem("Tệp phụ đề đang rỗng.");
      return;
    }
    setSubtitleFile(file);
  }

  async function cancelUpload() {
    if (uploadFinalizingRef.current) {
      setNotice("Đang tạo job từ file đã tải xong; hãy hủy job sau khi nó xuất hiện.");
      return;
    }
    uploadCancelledRef.current = true;
    if (!uploadSessionIdRef.current && uploadProgress?.phase === "preparing") {
      setNotice(
        "Đang chờ máy chủ cấp mã phiên; dữ liệu sẽ được xóa ngay khi nhận được mã.",
      );
    }
    uploadAbortRef.current?.abort();
    uploadRequestRef.current?.abort();
    setUploadProgress((current) => ({
      phase: "cancelling",
      filename: current?.filename,
      fileLoaded: current?.fileLoaded ?? 0,
      fileTotal: current?.fileTotal ?? 0,
      overallLoaded: current?.overallLoaded ?? 0,
      overallTotal: current?.overallTotal ?? 0,
      speedBytesPerSecond: 0,
    }));
  }

  async function discardRetainedUpload() {
    const sessionId = uploadSessionIdRef.current;
    if (!sessionId || submitting) return;
    setSubmitting(true);
    setProblem(null);
    setNotice(null);
    try {
      await api<void>(`/uploads/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      uploadSessionIdRef.current = null;
      uploadRequestFingerprintRef.current = null;
      uploadedMediaFileRef.current = null;
      uploadedSubtitleFileRef.current = null;
      setUploadSessionId(null);
      setNotice("Đã xóa phiên tạm; bạn có thể đổi cấu hình và tải lại.");
    } catch (error) {
      if (error instanceof ApiRequestError && error.code === "upload_not_found") {
        uploadSessionIdRef.current = null;
        uploadRequestFingerprintRef.current = null;
        uploadedMediaFileRef.current = null;
        uploadedSubtitleFileRef.current = null;
        setUploadSessionId(null);
        setNotice("Phiên tạm đã được máy chủ tự dọn; bạn có thể đổi cấu hình.");
      } else {
        setProblem(messageOf(error, "Không thể xóa phiên tải tạm."));
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!mediaFile) {
      setProblem("Hãy chọn một video MP4 hoặc MKV từ thiết bị.");
      return;
    }
    if (subtitleFile && sourceLanguage === "auto") {
      setProblem("Khi dùng SRT thủ công, hãy chọn ngôn ngữ nguồn cụ thể thay vì Tự động.");
      return;
    }
    if (!rightsConfirmed) {
      setProblem("Bạn cần xác nhận có quyền tải và xử lý nội dung.");
      return;
    }
    const hasVoiceSelection = Boolean(voiceId.trim() || voiceReferencePath.trim() || speakerVoicesText.trim());
    if (hasVoiceSelection && !voiceRightsConfirmed) {
      setProblem("Bạn cần xác nhận có quyền sử dụng giọng tham chiếu.");
      return;
    }
    setSubmitting(true);
    setProblem(null);
    setNotice(null);
    uploadCancelledRef.current = false;
    uploadFinalizingRef.current = false;
    const uploadAbort = new AbortController();
    uploadAbortRef.current = uploadAbort;
    let createdUploadId: string | null = null;
    const models = {
      asr: selectedModels.asr || null,
      translation: translationBackend === "openai" ? null : (selectedModels.mt || null),
      separation: selectedModels.separation || null,
      tts: selectedModels.tts || null,
    };
    const voice = hasVoiceSelection
      ? {
          voice_id: voiceId.trim() || null,
          reference_path: voiceReferencePath.trim() || null,
        }
      : null;
    try {
      const glossary = parseGlossary(glossaryText);
      const speakerVoices = parseSpeakerVoices(speakerVoicesText);
        const overallTotal = mediaFile.size + (subtitleFile?.size ?? 0);
        const retainedUploadId = uploadSessionIdRef.current;
        createdUploadId = retainedUploadId;
        setUploadProgress({
          phase: "preparing",
          fileLoaded: 0,
          fileTotal: mediaFile.size,
          overallLoaded: 0,
          overallTotal,
          speedBytesPerSecond: 0,
        });
        const uploadRequest = {
            media_filename: mediaFile.name,
            ...(subtitleFile ? { subtitle_filename: subtitleFile.name } : {}),
            rights_confirmed: true,
            source_language: sourceLanguage,
            timing_profile: timingProfile,
            models,
            voice,
            voice_rights_confirmed: hasVoiceSelection && voiceRightsConfirmed,
            glossary,
            translation_backend: translationBackend,
            translation_model: translationBackend === "openai" ? (openaiModel.trim() || null) : null,
            require_final_approval: requireFinalApproval,
            speaker_voices: speakerVoices,
            diarize_speakers: diarizeSpeakers,
            auto_speaker_voices: autoSpeakerVoices,
            qc_enabled: qcEnabled,
        };
        const uploadRequestFingerprint = JSON.stringify({
          rights_confirmed: uploadRequest.rights_confirmed,
          source_language: uploadRequest.source_language,
          timing_profile: uploadRequest.timing_profile,
          models: uploadRequest.models,
          voice: uploadRequest.voice,
          voice_rights_confirmed: uploadRequest.voice_rights_confirmed,
          glossary: uploadRequest.glossary,
          translation_backend: uploadRequest.translation_backend,
          translation_model: uploadRequest.translation_model,
          require_final_approval: uploadRequest.require_final_approval,
          speaker_voices: uploadRequest.speaker_voices,
          diarize_speakers: uploadRequest.diarize_speakers,
          auto_speaker_voices: uploadRequest.auto_speaker_voices,
          qc_enabled: uploadRequest.qc_enabled,
          subtitle_declared: Boolean(uploadRequest.subtitle_filename),
        });
        let session: UploadSession | null = null;
        if (retainedUploadId) {
          if (
            uploadRequestFingerprintRef.current &&
            uploadRequestFingerprintRef.current !== uploadRequestFingerprint
          ) {
            throw new ApiRequestError(
              "Cấu hình đã thay đổi so với phiên đang giữ. Hãy xóa phiên tạm trước khi tạo phiên mới.",
              "upload_configuration_changed",
              true,
            );
          }
          try {
            session = await api<UploadSession>(
              `/uploads/${encodeURIComponent(retainedUploadId)}`,
              { signal: uploadAbort.signal },
            );
          } catch (error) {
            if (!(error instanceof ApiRequestError) || error.code !== "upload_not_found") {
              throw error;
            }
            uploadSessionIdRef.current = null;
            uploadRequestFingerprintRef.current = null;
            uploadedMediaFileRef.current = null;
            uploadedSubtitleFileRef.current = null;
            setUploadSessionId(null);
          }
        }
        if (session && (
          session.media_filename !== mediaFile.name ||
          Boolean(session.subtitle_filename) !== Boolean(subtitleFile)
        )) {
          throw new ApiRequestError(
            "Tệp đã chọn không khớp phiên đang giữ. Hãy bấm Xóa phiên tạm trước khi đổi video hoặc trạng thái SRT.",
            "upload_configuration_changed",
            true,
          );
        }
        if (!session) {
          session = await api<UploadSession>("/uploads", {
            method: "POST",
            body: JSON.stringify(uploadRequest),
          });
          uploadRequestFingerprintRef.current = uploadRequestFingerprint;
        }
        createdUploadId = session.id;
        uploadSessionIdRef.current = session.id;
        setUploadSessionId(session.id);
        // Session creation is intentionally not aborted: once the server has
        // reserved durable storage we need its ID so cancellation can delete
        // it instead of leaving an unknown orphan until TTL cleanup.
        if (uploadCancelledRef.current) {
          throw new UploadCancelledError();
        }
        const mediaReady = session.media_size_bytes === mediaFile.size &&
          uploadedMediaFileRef.current === mediaFile;
        const subtitleReady = !subtitleFile || (
          session.subtitle_size_bytes === subtitleFile.size &&
          uploadedSubtitleFileRef.current === subtitleFile
        );
        if (!mediaReady) {
          await uploadBinary(
            `/v1/uploads/${encodeURIComponent(session.id)}/media`,
            mediaFile,
            "media",
            0,
            overallTotal,
            uploadRequestRef,
            setUploadProgress,
          );
          uploadedMediaFileRef.current = mediaFile;
        }
        if (subtitleFile && !subtitleReady) {
          await uploadBinary(
            `/v1/uploads/${encodeURIComponent(session.id)}/subtitle`,
            subtitleFile,
            "subtitle",
            mediaFile.size,
            overallTotal,
            uploadRequestRef,
            setUploadProgress,
          );
          uploadedSubtitleFileRef.current = subtitleFile;
        }
        uploadFinalizingRef.current = true;
        setUploadProgress({
          phase: "finalizing",
          fileLoaded: subtitleFile?.size ?? mediaFile.size,
          fileTotal: subtitleFile?.size ?? mediaFile.size,
          overallLoaded: overallTotal,
          overallTotal,
          speedBytesPerSecond: 0,
        });
        const job = await api<Job>(`/uploads/${encodeURIComponent(session.id)}/finalize`, {
          method: "POST",
        });
        uploadFinalizingRef.current = false;
        uploadSessionIdRef.current = null;
        uploadRequestFingerprintRef.current = null;
        uploadedMediaFileRef.current = null;
        uploadedSubtitleFileRef.current = null;
        setUploadSessionId(null);
      setSelectedJobId(job.id);
      setNotice(`Đã tải tệp và tạo job ${shortId(job.id)}. Tiến trình sẽ tự cập nhật.`);
      await refreshOverview();
    } catch (error) {
      const cancelled = uploadCancelledRef.current || error instanceof UploadCancelledError ||
        (error instanceof DOMException && error.name === "AbortError");
      const shouldDeleteUpload = cancelled;
      let uploadCleanupError: unknown = null;
      if (createdUploadId && uploadSessionIdRef.current && shouldDeleteUpload) {
        try {
          await api<void>(`/uploads/${encodeURIComponent(createdUploadId)}`, { method: "DELETE" });
        } catch (cleanupError) {
          if (!(cleanupError instanceof ApiRequestError) || cleanupError.code !== "upload_not_found") {
            uploadCleanupError = cleanupError;
          }
        }
        if (uploadCleanupError === null) {
          uploadSessionIdRef.current = null;
          uploadRequestFingerprintRef.current = null;
          uploadedMediaFileRef.current = null;
          uploadedSubtitleFileRef.current = null;
          setUploadSessionId(null);
        }
      }
      if (cancelled) {
        if (uploadCleanupError === null && createdUploadId) {
          setNotice("Đã hủy tải tệp và xóa dữ liệu tạm trên máy chủ.");
        } else if (uploadCleanupError === null) {
          setProblem(
            "Đã yêu cầu hủy trước khi nhận được mã phiên. Nếu máy chủ đã nhận yêu cầu nhưng phản hồi bị mất, session chưa finalize sẽ tự dọn theo TTL máy chủ (mặc định 7 ngày).",
          );
        } else {
          const retainedId = uploadSessionIdRef.current;
          setProblem(
            `Đã dừng gửi file nhưng chưa thể xóa dữ liệu tạm${retainedId ? ` của phiên ${shortId(retainedId)}` : ""}. Hãy thử xóa phiên lại.`,
          );
        }
      } else {
        const retained = uploadSessionIdRef.current
          ? ` Phiên ${shortId(uploadSessionIdRef.current)} được giữ lại; bấm Bắt đầu để thử lại mà không gửi lại file đã hoàn tất.`
          : "";
        setProblem(`${messageOf(error, "Không thể tải tệp và tạo job.")}${retained}`);
      }
    } finally {
      uploadFinalizingRef.current = false;
      uploadAbortRef.current = null;
      uploadRequestRef.current = null;
      setUploadProgress(null);
      setSubmitting(false);
    }
  }

  async function jobMutation(job: Job, action: "cancel" | "resume" | "refresh") {
    const key = `${job.id}:${action}`;
    setBusyAction(key);
    setProblem(null);
    try {
      await api<Job>(`/jobs/${encodeURIComponent(job.id)}/${action}`, { method: "POST" });
      await refreshOverview();
    } catch (error) {
      setProblem(messageOf(error, "Không thể cập nhật job."));
    } finally {
      setBusyAction(null);
    }
  }

  async function selectLanguage(job: Job, language: string) {
    const key = `${job.id}:language`;
    setBusyAction(key);
    setProblem(null);
    try {
      await api<Job>(`/jobs/${encodeURIComponent(job.id)}/language`, {
        method: "POST",
        body: JSON.stringify({ language }),
      });
      setNotice(`Đã chọn ${language} làm ngôn ngữ nguồn.`);
      await refreshOverview();
    } catch (error) {
      setProblem(messageOf(error, "Không thể chọn ngôn ngữ."));
    } finally {
      setBusyAction(null);
    }
  }

  async function selectSubtitle(job: Job, subtitleId?: string) {
    const suffix = subtitleId
      ? `/subtitles/${encodeURIComponent(subtitleId)}`
      : "/subtitles/use-asr";
    const key = `${job.id}:subtitle`;
    setBusyAction(key);
    setProblem(null);
    try {
      await api<Job>(`/jobs/${encodeURIComponent(job.id)}${suffix}`, { method: "POST" });
      setNotice(subtitleId ? "Đã chọn phụ đề cho job." : "Đã chuyển sang nhận dạng lời nói bằng ASR.");
      await refreshOverview();
    } catch (error) {
      setProblem(messageOf(error, "Không thể xác nhận nguồn lời thoại."));
    } finally {
      setBusyAction(null);
    }
  }

  const studioJobId = selectedJob?.id ?? null;

  function syncStudioDrafts(view: StudioView) {
    const nextCast: Record<string, StudioCastDraft> = {};
    for (const member of view.cast) {
      nextCast[member.speaker] = {
        display_name: member.display_name ?? "",
        voice_id: member.voice_id ?? "",
        reference_path: member.reference_path ?? "",
      };
    }
    setCastDraft(nextCast);
    const nextScript: Record<number, string> = {};
    for (const line of view.script) {
      nextScript[line.ordinal] = line.text;
    }
    setScriptDraft(nextScript);
    setMixDraft({
      narration_lufs: view.mix.narration_lufs?.toString() ?? "",
      accompaniment_lufs: view.mix.accompaniment_lufs?.toString() ?? "",
      ducking_ratio: view.mix.ducking_ratio?.toString() ?? "",
      ducking_threshold: view.mix.ducking_threshold?.toString() ?? "",
      true_peak_db: view.mix.true_peak_db?.toString() ?? "",
    });
  }

  const refreshStudio = useCallback(async (jobId: string) => {
    setStudioError(null);
    try {
      const view = await api<StudioView>(`/jobs/${encodeURIComponent(jobId)}/studio`);
      setStudio(view);
      syncStudioDrafts(view);
    } catch (error) {
      setStudio(null);
      setStudioError(messageOf(error, "Không thể tải Studio lồng tiếng."));
    }
  }, []);

  useEffect(() => {
    if (!studioJobId) {
      setStudio(null);
      setStudioError(null);
      setStudioNotice(null);
      return;
    }
    setStudio(null);
    setStudioNotice(null);
    setTranslateKeyMissing(false);
    void refreshStudio(studioJobId);
  }, [studioJobId, refreshStudio]);

  const refreshReview = useCallback(async (jobId: string) => {
    setReviewError(null);
    try {
      const view = await api<ReviewView>(`/jobs/${encodeURIComponent(jobId)}/review`);
      setReview(view);
    } catch (error) {
      setReview(null);
      setReviewError(messageOf(error, "Không thể tải bản duyệt."));
    }
  }, []);

  useEffect(() => {
    if (selectedJob?.status === "awaiting_approval") {
      setRejectNote("");
      void refreshReview(selectedJob.id);
    } else {
      setReview(null);
      setReviewError(null);
      setReviewNotice(null);
    }
  }, [selectedJob?.id, selectedJob?.status, refreshReview]);

  async function saveStudioCast(jobId: string) {
    if (!studio) return;
    setStudioBusy("cast");
    setStudioError(null);
    setStudioNotice(null);
    try {
      const clean = (value: string) => {
        const trimmed = value.trim();
        return trimmed ? trimmed : null;
      };
      const result = await api<StudioCastResponse>(
        `/jobs/${encodeURIComponent(jobId)}/studio/cast`,
        {
          method: "PUT",
          body: JSON.stringify({
            speakers: studio.cast.map((member) => {
              const draft = castDraft[member.speaker] ?? {
                display_name: member.display_name ?? "",
                voice_id: member.voice_id ?? "",
                reference_path: member.reference_path ?? "",
              };
              return {
                speaker: member.speaker,
                display_name: clean(draft.display_name),
                voice_id: clean(draft.voice_id),
                reference_path: clean(draft.reference_path),
              };
            }),
          }),
        },
      );
      if (result && typeof result === "object" && "cast" in result) {
        setStudio(result);
        syncStudioDrafts(result);
      } else {
        await refreshStudio(jobId);
      }
      setStudioNotice(result.requeued ? STUDIO_REQUEUE_NOTICE : "Đã lưu bảng cast.");
      await refreshOverview();
    } catch (error) {
      setStudioError(messageOf(error, "Không thể lưu bảng cast."));
    } finally {
      setStudioBusy(null);
    }
  }

  async function saveStudioLine(jobId: string, line: StudioScriptLine) {
    const key = `line:${line.ordinal}`;
    setStudioBusy(key);
    setStudioError(null);
    setStudioNotice(null);
    try {
      const result = await api<{ ordinal: number; text: string; requeued?: boolean }>(
        `/jobs/${encodeURIComponent(jobId)}/studio/script/${line.ordinal}`,
        {
          method: "PUT",
          body: JSON.stringify({ text: scriptDraft[line.ordinal] ?? line.text }),
        },
      );
      setStudio((current) =>
        current
          ? {
              ...current,
              script: current.script.map((item) =>
                item.ordinal === result.ordinal ? { ...item, text: result.text } : item,
              ),
            }
          : current,
      );
      setScriptDraft((current) => ({ ...current, [result.ordinal]: result.text }));
      setStudioNotice(result.requeued ? STUDIO_REQUEUE_NOTICE : "Đã lưu câu thoại.");
      await refreshOverview();
    } catch (error) {
      setStudioError(messageOf(error, "Không thể lưu câu thoại."));
    } finally {
      setStudioBusy(null);
    }
  }

  async function retakeStudioLines(jobId: string, ordinals: number[]) {
    const key = `retake:${ordinals.join(",")}`;
    setStudioBusy(key);
    setStudioError(null);
    setStudioNotice(null);
    try {
      const result = await api<{ retaken?: number[]; requeued?: boolean }>(
        `/jobs/${encodeURIComponent(jobId)}/studio/retake`,
        { method: "POST", body: JSON.stringify({ ordinals }) },
      );
      await refreshStudio(jobId);
      const count = result.retaken?.length ?? ordinals.length;
      setStudioNotice(
        result.requeued ? STUDIO_REQUEUE_NOTICE : `Đã gửi thu lại ${count} câu.`,
      );
      await refreshOverview();
    } catch (error) {
      setStudioError(messageOf(error, "Không thể gửi thu lại."));
    } finally {
      setStudioBusy(null);
    }
  }

  async function saveStudioMix(jobId: string) {
    setStudioBusy("mix");
    setStudioError(null);
    setStudioNotice(null);
    try {
      const numeric = (value: string) => {
        const trimmed = value.trim();
        if (!trimmed) return undefined;
        const parsed = Number(trimmed);
        return Number.isFinite(parsed) ? parsed : undefined;
      };
      const result = await api<{ mix?: StudioMix; requeued?: boolean }>(
        `/jobs/${encodeURIComponent(jobId)}/studio/mix`,
        {
          method: "PUT",
          body: JSON.stringify({
            narration_lufs: numeric(mixDraft.narration_lufs),
            accompaniment_lufs: numeric(mixDraft.accompaniment_lufs),
            ducking_ratio: numeric(mixDraft.ducking_ratio),
            ducking_threshold: numeric(mixDraft.ducking_threshold),
            true_peak_db: numeric(mixDraft.true_peak_db),
          }),
        },
      );
      if (result.mix) {
        setStudio((current) => (current ? { ...current, mix: result.mix as StudioMix } : current));
        setMixDraft({
          narration_lufs: result.mix.narration_lufs?.toString() ?? "",
          accompaniment_lufs: result.mix.accompaniment_lufs?.toString() ?? "",
          ducking_ratio: result.mix.ducking_ratio?.toString() ?? "",
          ducking_threshold: result.mix.ducking_threshold?.toString() ?? "",
          true_peak_db: result.mix.true_peak_db?.toString() ?? "",
        });
      }
      setStudioNotice(result.requeued ? STUDIO_REQUEUE_NOTICE : "Đã lưu thông số mix.");
      await refreshOverview();
    } catch (error) {
      setStudioError(messageOf(error, "Không thể lưu thông số mix."));
    } finally {
      setStudioBusy(null);
    }
  }

  async function saveOpenSubtitles(event: FormEvent) {
    event.preventDefault();
    if (!openSubApiKey.trim() || !openSubUsername.trim() || !openSubPassword) {
      setIntegrationProblem("Hãy nhập API key, tên đăng nhập và mật khẩu OpenSubtitles.");
      return;
    }
    setIntegrationBusy("opensubtitles");
    setIntegrationProblem(null);
    setIntegrationNotice(null);
    try {
      const result = await api<{ message?: string; restart_required?: boolean }>(
        "/admin/opensubtitles",
        {
          method: "PUT",
          body: JSON.stringify({
            api_key: openSubApiKey.trim(),
            username: openSubUsername.trim(),
            password: openSubPassword,
          }),
        },
        true,
      );
      setOpenSubApiKey("");
      setOpenSubPassword("");
      setIntegrationNotice(
        result.message ||
          (result.restart_required
            ? "Đã lưu an toàn. Hãy chạy “dub stack restart” để dịch vụ dùng cấu hình mới."
            : "Đã xác thực và lưu cấu hình OpenSubtitles."),
      );
      await refreshIntegrations();
    } catch (error) {
      setIntegrationProblem(messageOf(error, "Không thể cấu hình OpenSubtitles."));
    } finally {
      setIntegrationBusy(null);
    }
  }

  async function removeOpenSubtitles() {
    if (!window.confirm("Xóa API key, token và tuyến API OpenSubtitles đã lưu trên máy chủ?")) return;
    setIntegrationBusy("opensubtitles-delete");
    setIntegrationProblem(null);
    setIntegrationNotice(null);
    try {
      const result = await api<{ message?: string; restart_required?: boolean }>(
        "/admin/opensubtitles",
        {
          method: "DELETE",
          body: JSON.stringify({ confirm: "DELETE_OPENSUBTITLES_CREDENTIALS" }),
        },
        true,
      );
      setIntegrationNotice(
        result.message || "Đã xóa bí mật OpenSubtitles. Khởi động lại stack để áp dụng.",
      );
      await refreshIntegrations();
    } catch (error) {
      setIntegrationProblem(messageOf(error, "Không thể xóa cấu hình OpenSubtitles."));
    } finally {
      setIntegrationBusy(null);
    }
  }

  async function saveOpenAiKey(event: FormEvent) {
    event.preventDefault();
    if (!openaiKeyInput.trim()) {
      setSettingsProblem("Hãy nhập key OpenAI trước khi lưu.");
      return;
    }
    setSettingsBusy("openai-key");
    setSettingsProblem(null);
    setSettingsNotice(null);
    try {
      await api<{ configured: boolean }>("/settings/openai-key", {
        method: "PUT",
        body: JSON.stringify({ key: openaiKeyInput.trim() }),
      });
      setOpenaiKeyInput("");
      setSettingsNotice("Đã lưu key OpenAI phía máy chủ. Key không bao giờ hiển thị lại trên dashboard.");
      await refreshOpenAiKey();
    } catch (error) {
      setSettingsProblem(messageOf(error, "Không thể lưu key OpenAI."));
    } finally {
      setSettingsBusy(null);
    }
  }

  async function removeOpenAiKey() {
    if (!window.confirm("Xóa key OpenAI đã lưu trên máy chủ? Job dịch API sau đó sẽ báo thiếu key.")) return;
    setSettingsBusy("openai-key-delete");
    setSettingsProblem(null);
    setSettingsNotice(null);
    try {
      await api<{ configured: boolean }>("/settings/openai-key", { method: "DELETE" });
      setSettingsNotice("Đã xóa key OpenAI.");
      await refreshOpenAiKey();
    } catch (error) {
      setSettingsProblem(messageOf(error, "Không thể xóa key OpenAI."));
    } finally {
      setSettingsBusy(null);
    }
  }

  async function translateWithApi(job: Job) {
    const key = `${job.id}:translate`;
    setBusyAction(key);
    setTranslateKeyMissing(false);
    setProblem(null);
    try {
      const result = await api<{ job_id: string; status: string; model?: string }>(
        `/jobs/${encodeURIComponent(job.id)}/translate-openai`,
        { method: "POST" },
      );
      setNotice(
        `Đã gửi yêu cầu dịch bằng API${result.model ? ` (model ${result.model})` : ""}. Trang tự cập nhật; khi dịch xong job chuyển sang tạo giọng.`,
      );
      await refreshOverview();
    } catch (error) {
      if (error instanceof ApiRequestError && error.code === "openai_api_key_missing") {
        setTranslateKeyMissing(true);
      } else {
        setProblem(messageOf(error, "Không thể bắt đầu dịch bằng API."));
      }
    } finally {
      setBusyAction(null);
    }
  }

  async function approveReview(jobId: string) {
    setReviewBusy("approve");
    setReviewError(null);
    setReviewNotice(null);
    try {
      await api<Job>(`/jobs/${encodeURIComponent(jobId)}/review/approve`, { method: "POST" });
      setReviewNotice("Đã duyệt. Job chuyển sang hoàn tất; file xuất hiện ở mục tải kết quả bên dưới.");
      await refreshOverview();
    } catch (error) {
      setReviewError(messageOf(error, "Không thể duyệt kết quả."));
    } finally {
      setReviewBusy(null);
    }
  }

  async function rejectReview(jobId: string) {
    setReviewBusy("reject");
    setReviewError(null);
    setReviewNotice(null);
    try {
      const note = rejectNote.trim();
      await api<Job>(`/jobs/${encodeURIComponent(jobId)}/review/reject`, {
        method: "POST",
        body: JSON.stringify(note ? { note } : {}),
      });
      setReviewNotice("Đã gửi yêu cầu sửa. Job quay lại Studio để bạn sửa rồi dựng lại.");
      await refreshOverview();
      await refreshStudio(jobId);
    } catch (error) {
      setReviewError(messageOf(error, "Không thể gửi yêu cầu sửa."));
    } finally {
      setReviewBusy(null);
    }
  }

  const cpuMode = health?.compute_mode === "cpu";
  const gpuDevices = health?.gpu?.gpus ?? [];
  const gpuWarnings = (cpuMode ? [] : health?.gpu?.warnings ?? [])
    .filter((warning): warning is string => typeof warning === "string" && warning.trim().length > 0)
    .map(gpuWarningMessage);
  const gpuSupportTier = health?.gpu?.support_tier ?? null;
  const gpuSupportTierLabel: Record<GpuSupportTier, string> = {
    supported: "Được hỗ trợ",
    "maintenance-limited": "Bảo trì giới hạn",
    experimental: "Thử nghiệm",
  };
  const gpuSupportHasRestrictions = !cpuMode &&
    (gpuSupportTier === "maintenance-limited" || gpuSupportTier === "experimental");
  const apiConnected = health !== null;
  const gpuReady = health?.gpu?.ready === true;
  const gpuHasWarnings = gpuWarnings.length > 0;
  const processorReady =
    health?.status === "ok" && gpuReady && !gpuHasWarnings && !gpuSupportHasRestrictions;
  const processorDegraded = apiConnected && !processorReady;
  const healthStateClass = processorReady ? "online" : processorDegraded ? "degraded" : "offline";
  const healthLabel = !apiConnected
    ? "Chưa kết nối API"
    : processorReady
      ? "Máy xử lý sẵn sàng"
      : gpuReady && (gpuHasWarnings || gpuSupportHasRestrictions)
        ? "Máy xử lý có cảnh báo"
      : gpuReady
        ? "Máy xử lý cần cấu hình"
        : cpuMode ? "CPU chưa sẵn sàng" : "GPU chưa sẵn sàng";
  const gpuHeading = cpuMode
    ? "CPU compatibility mode"
    : gpuDevices.length === 1
    ? gpuDevices[0].name || "GPU NVIDIA CUDA"
    : gpuDevices.length > 1
      ? `${gpuDevices.length} GPU NVIDIA CUDA`
      : apiConnected
        ? "GPU chưa sẵn sàng"
        : "GPU chưa kết nối";
  const subtitleCandidates = asArray<SubtitleCandidate>(selectedJob?.details?.subtitle_candidates);
  const languageCandidates = asArray<LanguageCandidate>(
    selectedJob?.details?.language_detection_candidates ?? selectedJob?.details?.language_candidates,
  );
  const warnings = warningMessages(
    selectedJob?.details?.warnings,
    selectedJob?.details?.subtitle_warnings,
    selectedJob?.result?.warnings,
  );
  const selectedProgress = selectedJob ? jobProgressView(selectedJob) : null;
  const openSubStatus = integrations?.opensubtitles;

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="Lồng Tiếng GPU — về đầu trang">
          <span className="brand-mark">LT</span>
          <span>
            <strong>Lồng Tiếng</strong>
            <small>GPU studio</small>
          </span>
        </a>
        <nav className="topnav" aria-label="Điều hướng dashboard">
          <a href="#workflow">Workflow</a>
          <a href="#settings">Cài đặt</a>
          <a href="#jobs">Job</a>
          <a href="#models">Model</a>
          <a href="#integrations">Tích hợp</a>
        </nav>
        <div className={`health-pill ${healthStateClass}`}>
          <span className="pulse" aria-hidden="true" />
          {healthLabel}
        </div>
      </header>

      <nav className="mobile-nav" aria-label="Điều hướng nhanh">
        <a href="#workflow">Tải video</a>
        <a href="#settings">Cài đặt</a>
        <a href="#jobs">Job</a>
        <a href="#models">Model</a>
        <a href="#integrations">Phụ đề</a>
      </nav>

      <section className="hero" id="top">
        <div className="hero-copy">
          <p className="eyebrow">THUYẾT MINH NGOẠI TUYẾN · CPU / NVIDIA CUDA</p>
          <h1>Biến một bản phim thành bản thuyết minh Việt.</h1>
          <p className="lead">
            Tách lời diễn viên, giữ nhạc và hiệu ứng, dịch cục bộ rồi dựng lại một track
            thuyết minh hoàn chỉnh — không gửi nội dung lên dịch vụ AI.
          </p>
        </div>
        <div className={`machine-card ${processorReady ? "ready" : "degraded"}`} aria-label="Trạng thái máy xử lý">
          <span className="machine-label">MÁY XỬ LÝ</span>
          <strong>{gpuHeading}</strong>
          <div className="machine-meta">
            <span>{cpuMode ? "CUDA đã tắt · chế độ rất chậm" : gpuDevices.length ? `${gpuDevices.length} GPU được phát hiện` : "Đang kiểm tra GPU"}</span>
            <span>{validModelCount}/{catalog.models.length} model sẵn sàng</span>
          </div>
          {gpuDevices.length > 0 && (
            <div className="gpu-device-list" aria-label="GPU NVIDIA CUDA được phát hiện">
              {gpuDevices.map((gpu, index) => {
                const name = gpu.name || `GPU ${index + 1}`;
                const details = [
                  gpu.compute_capability ? `CC ${gpu.compute_capability}` : "CC chưa rõ",
                  formatGpuMemory(gpu.memory_total_mib),
                  gpu.driver_version ? `driver ${gpu.driver_version}` : null,
                ].filter(Boolean).join(" · ");
                return (
                  <div className="gpu-device" key={gpu.uuid || `${name}-${index}`}>
                    <span className="gpu-device-name">{name}</span>
                    <small>{details}</small>
                  </div>
                );
              })}
            </div>
          )}
          <div className="machine-flags">
            <span className={gpuReady ? "ok" : "warn"}>{cpuMode ? "CPU" : "GPU"} {gpuReady ? "sẵn sàng" : "chưa sẵn sàng"}</span>
            {gpuReady && !cpuMode && (
              <span className={gpuSupportTier === "supported" ? "ok" : "warn"}>
                Mức hỗ trợ: {gpuSupportTier ? gpuSupportTierLabel[gpuSupportTier] : "Không xác định"}
              </span>
            )}
            <span className="ok">AI đám mây tắt theo thiết kế (trừ bước dịch API nếu bạn bật)</span>
          </div>
          {validModelCount < catalog.models.length && (
            <a className="machine-action" href="#models">Thiết lập model</a>
          )}
          {gpuWarnings.length > 0 && (
            <div className="gpu-health-warnings" role="status" aria-label="Cảnh báo GPU">
              <strong>Cảnh báo GPU</strong>
              <ul>
                {gpuWarnings.map((warning, index) => (
                  <li key={`${warning}-${index}`}>{warning}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </section>

      <section className="workspace" id="workflow">
        <div className="composer panel">
          <div className="panel-heading">
            <div>
              <span className="step-number">01</span>
              <h2>Chọn video</h2>
            </div>
            <span className="legal-note">Chỉ nội dung bạn có quyền sử dụng</span>
          </div>

          <div className="upload-source" aria-label="Tải video và phụ đề thủ công">
              <div className="upload-file-grid">
                <label className={`file-picker ${mediaFile ? "selected" : ""}`}>
                  <input
                    ref={mediaInputRef}
                    type="file"
                    accept=".mp4,.mkv,video/mp4,video/x-matroska"
                    disabled={submitting || uploadConfigurationLocked}
                    onChange={selectMediaFile}
                  />
                  <span className="file-picker-kicker">VIDEO BẮT BUỘC</span>
                  <strong>{mediaFile ? mediaFile.name : "Chọn MP4 hoặc MKV"}</strong>
                  <small>{mediaFile ? formatBytes(mediaFile.size) : "Tệp được truyền thẳng tới máy GPU"}</small>
                  <span className="file-picker-action">{mediaFile ? "Đổi video" : "Duyệt tệp"}</span>
                </label>
                <label className={`file-picker ${subtitleFile ? "selected" : ""}`}>
                  <input
                    ref={subtitleInputRef}
                    type="file"
                    accept=".srt,application/x-subrip,text/plain"
                    disabled={submitting || uploadConfigurationLocked}
                    onChange={selectSubtitleFile}
                  />
                  <span className="file-picker-kicker">PHỤ ĐỀ TÙY CHỌN</span>
                  <strong>{subtitleFile ? subtitleFile.name : "Chọn SRT thủ công"}</strong>
                  <small>{subtitleFile ? formatBytes(subtitleFile.size) : "Để trống nếu muốn nhận dạng bằng ASR"}</small>
                  <span className="file-picker-action">{subtitleFile ? "Đổi phụ đề" : "Duyệt tệp"}</span>
                </label>
              </div>
              <p className="helper-copy">
                Với MP4/MKV, H.264/AVC được giữ nguyên không mã hóa lại; HEVC SDR được tự động
                chuyển mã sang H.264/AVC. HEVC HDR10, HLG hoặc Dolby Vision chưa được hỗ trợ;
                AV1, VP9, VP8 và FFV1 bị từ chối. Ảnh bìa nhúng và thumbnail được tự động bỏ qua.
              </p>
              <div className="upload-file-summary">
                <span>
                  {mediaFile
                    ? `${mediaFile.name} · ${formatBytes(mediaFile.size)}`
                    : "Chưa chọn video"}
                </span>
                {subtitleFile && (
                  <button
                    type="button"
                    disabled={submitting || uploadConfigurationLocked}
                    onClick={() => {
                      setSubtitleFile(null);
                      if (subtitleInputRef.current) subtitleInputRef.current.value = "";
                    }}
                  >
                    Bỏ SRT
                  </button>
                )}
              </div>
              <p className="upload-helper">
                Video giữ nguyên trên máy chủ này. Có SRT thì cần chọn đúng ngôn ngữ nguồn; không có SRT, hệ thống sẽ dùng ASR.
              </p>
              {uploadSessionId && !uploadProgress && (
                <div className="upload-file-summary">
                  <span>
                    Phiên {shortId(uploadSessionId)} đang được giữ; file đã gửi xong sẽ được bỏ qua. Cấu hình bị khóa theo phiên này.
                  </span>
                  <button type="button" disabled={submitting} onClick={() => void discardRetainedUpload()}>
                    Xóa phiên tạm
                  </button>
                </div>
              )}
            </div>

          <div className="divider" />

          <div className="panel-heading compact">
            <div>
              <span className="step-number">02</span>
              <h2>Cấu hình bản tiếng Việt</h2>
            </div>
          </div>

          <form className="job-form" onSubmit={handleSubmit}>
            <div className="settings-grid">
              <label className="field">
                <span>Ngôn ngữ nguồn</span>
                <select
                  value={sourceLanguage}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setSourceLanguage(event.target.value)}
                >
                  {languageOptions.map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Nguồn lời thoại</span>
                <span className="static-field">
                  {subtitleFile ? "SRT thủ công đã chọn" : "Nhận dạng lời nói bằng ASR"}
                </span>
              </label>
            </div>

            <fieldset className="timing-profile" disabled={submitting || uploadConfigurationLocked}>
              <legend>Nhịp lời thuyết minh</legend>
              <label className={timingProfile === "natural" ? "selected" : ""}>
                <input
                  type="radio"
                  name="timing-profile"
                  value="natural"
                  checked={timingProfile === "natural"}
                  onChange={() => setTimingProfile("natural")}
                />
                <span>
                  <strong>Tự nhiên</strong>
                  <small>Ưu tiên tốc độ nói ổn định, mượn khoảng lặng và cho phép lệch nhẹ để câu dễ nghe.</small>
                </span>
                <em>Mặc định</em>
              </label>
              <label className={timingProfile === "strict" ? "selected" : ""}>
                <input
                  type="radio"
                  name="timing-profile"
                  value="strict"
                  checked={timingProfile === "strict"}
                  onChange={() => setTimingProfile("strict")}
                />
                <span>
                  <strong>Khớp chặt</strong>
                  <small>Bám sát từng mốc phụ đề; tốc độ giữa các câu có thể thay đổi rõ hơn.</small>
                </span>
              </label>
            </fieldset>

            <details className="advanced-config">
              <summary>Model và giọng nói nâng cao</summary>
              <div className="model-select-grid">
                {modelStages.map(([stage, label]) => (
                  <label className="field" key={stage}>
                    <span>{label}</span>
                    <select
                      value={selectedModels[stage] ?? ""}
                      disabled={submitting || uploadConfigurationLocked || (stage === "mt" && translationBackend === "openai")}
                      onChange={(event) =>
                        setSelectedModels((current) => ({ ...current, [stage]: event.target.value }))
                      }
                    >
                      <option value="">Mặc định của máy chủ</option>
                      {modelsFor(stage).map((model) => (
                        <option key={model.id} value={model.id} disabled={!model.installed || !model.valid}>
                          {model.id}{model.installed && model.valid ? "" : " — chưa sẵn sàng"}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              <div className="voice-grid">
                <label className="field">
                  <span>Backend dịch</span>
                  <select
                    value={translationBackend}
                    disabled={submitting || uploadConfigurationLocked}
                    onChange={(event) =>
                      setTranslationBackend(event.target.value === "openai" ? "openai" : "local")
                    }
                  >
                    <option value="local">Cục bộ (Gemma trên máy)</option>
                    <option value="openai">API OpenAI</option>
                  </select>
                </label>
                <label className="field">
                  <span>Model API (khi chọn OpenAI)</span>
                  <input
                    value={openaiModel}
                    disabled={translationBackend !== "openai" || submitting || uploadConfigurationLocked}
                    onChange={(event) => setOpenaiModel(event.target.value)}
                    placeholder="gpt-4o-mini (để trống = mặc định)"
                  />
                </label>
              </div>
              {translationBackend === "openai" && (
                <p className="helper-copy">
                  Dịch bằng API gửi lời thoại lên OpenAI; model dịch cục bộ ở trên không dùng cho job này.
                  {openaiKeyConfigured === false
                    ? " Chưa có key OpenAI — mở Cài đặt để nhập key trước khi tạo job."
                    : openaiKeyConfigured
                      ? " Key OpenAI đã cấu hình trên máy chủ."
                      : " Trạng thái key chưa rõ — kiểm tra ở mục Cài đặt."}{" "}
                  <a href="#settings">Mở Cài đặt</a>
                </p>
              )}
              <label className="rights-check">
                <input
                  type="checkbox"
                  checked={requireFinalApproval}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setRequireFinalApproval(event.target.checked)}
                />
                <span>Dừng lại để mình duyệt kết quả trước khi xuất file cuối (job sẽ chờ ở trạng thái “Chờ duyệt kết quả”).</span>
              </label>
              <div className="voice-grid">
                <label className="field">
                  <span>Voice ID (tùy chọn)</span>
                  <input
                    value={voiceId}
                    disabled={submitting || uploadConfigurationLocked}
                    onChange={(event) => setVoiceId(event.target.value)}
                    placeholder="Preset giọng cục bộ"
                  />
                </label>
                <label className="field">
                  <span>Đường dẫn giọng tham chiếu trên server</span>
                  <input
                    value={voiceReferencePath}
                    disabled={submitting || uploadConfigurationLocked}
                    onChange={(event) => setVoiceReferencePath(event.target.value)}
                    placeholder="/workspace/voices/reference.wav"
                  />
                </label>
              </div>
              <label className="field">
                <span>Glossary nhất quán (tùy chọn)</span>
                <textarea
                  value={glossaryText}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setGlossaryText(event.target.value)}
                  placeholder={"Continental = Khách sạn Continental\nJohn Wick = John Wick"}
                  rows={4}
                />
                <small>Mỗi dòng: thuật ngữ nguồn = cách dịch tiếng Việt. Tối đa 100 mục.</small>
              </label>
              <label className="field">
                <span>Giọng theo nhân vật (tùy chọn)</span>
                <textarea
                  value={speakerVoicesText}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setSpeakerVoicesText(event.target.value)}
                  placeholder={"ANNA=3\nNAM=/workspace/voices/nam.wav"}
                  rows={3}
                />
                <small>Mỗi dòng: NHÃN=voice_id hoặc đường dẫn file giọng mẫu. Nhãn khớp tên nhân vật trong SRT hoặc diarization (VD: SPEAKER_00).</small>
              </label>
              <label className="rights-check">
                <input
                  type="checkbox"
                  checked={diarizeSpeakers}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setDiarizeSpeakers(event.target.checked)}
                />
                <span>Tự tách nhân vật bằng diarization (cần pipeline pyannote đã cài trên máy xử lý).</span>
              </label>
              <label className="rights-check">
                <input
                  type="checkbox"
                  checked={autoSpeakerVoices}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setAutoSpeakerVoices(event.target.checked)}
                />
                <span>Tự gán giọng Piper theo thứ tự nhân vật (chỉ áp dụng cho backend Piper).</span>
              </label>
              <label className="rights-check">
                <input
                  type="checkbox"
                  checked={qcEnabled}
                  disabled={submitting || uploadConfigurationLocked}
                  onChange={(event) => setQcEnabled(event.target.checked)}
                />
                <span>Cho AI nghe lại sau khi dựng xong và gắn cờ câu lệch kịch bản.</span>
              </label>
              {(voiceId || voiceReferencePath || speakerVoicesText.trim()) && (
                <label className="rights-check voice-rights">
                  <input
                    type="checkbox"
                    checked={voiceRightsConfirmed}
                    disabled={submitting || uploadConfigurationLocked}
                    onChange={(event) => setVoiceRightsConfirmed(event.target.checked)}
                  />
                  <span>Tôi có quyền sử dụng giọng hoặc bản ghi tham chiếu này.</span>
                </label>
              )}
              <p className="helper-copy">
                Chỉ model đã cài và xác minh mới chọn được. Cài profile bằng CLI: <code>dub models install-profile maximum --yes</code>.
              </p>
            </details>

            <label className="rights-check">
              <input
                type="checkbox"
                checked={rightsConfirmed}
                disabled={submitting || uploadConfigurationLocked}
                onChange={(event) => setRightsConfirmed(event.target.checked)}
              />
              <span>
                Tôi xác nhận mình sở hữu hoặc được phép tải, chỉnh sửa và xử lý nội dung này.
              </span>
            </label>

            {uploadProgress && (
              <section className="upload-progress" aria-live="polite" aria-label="Tiến độ tải tệp">
                <div className="upload-progress-heading">
                  <span>{uploadPhaseLabels[uploadProgress.phase]}</span>
                  <strong>
                    {uploadProgress.overallTotal > 0
                      ? `${clampPercent((uploadProgress.overallLoaded / uploadProgress.overallTotal) * 100).toFixed(1)}%`
                      : "Đang chuẩn bị"}
                  </strong>
                </div>
                <div
                  className="upload-progress-track"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={uploadProgress.overallTotal > 0
                    ? Math.round(clampPercent((uploadProgress.overallLoaded / uploadProgress.overallTotal) * 100))
                    : 0}
                >
                  <i style={{
                    width: `${uploadProgress.overallTotal > 0
                      ? clampPercent((uploadProgress.overallLoaded / uploadProgress.overallTotal) * 100)
                      : 0}%`,
                  }} />
                </div>
                <div className="upload-progress-metrics">
                  <span>
                    <small>Tệp hiện tại</small>
                    <strong>{uploadProgress.filename ?? "Đang khởi tạo…"}</strong>
                  </span>
                  <span>
                    <small>Đã gửi</small>
                    <strong>{formatBytes(uploadProgress.overallLoaded)} / {formatBytes(uploadProgress.overallTotal)}</strong>
                  </span>
                  <span>
                    <small>Tốc độ</small>
                    <strong>{uploadProgress.speedBytesPerSecond > 0
                      ? formatByteRate(uploadProgress.speedBytesPerSecond)
                      : "Đang đo…"}</strong>
                  </span>
                </div>
                {uploadSessionId && <code>Phiên {shortId(uploadSessionId)}</code>}
                <button
                  className="small-button danger upload-cancel"
                  type="button"
                  disabled={uploadProgress.phase === "cancelling" || uploadProgress.phase === "finalizing"}
                  onClick={() => void cancelUpload()}
                >
                  {uploadProgress.phase === "cancelling"
                    ? "Đang hủy…"
                    : uploadProgress.phase === "finalizing"
                      ? "Đang tạo job…"
                      : "Hủy tải tệp"}
                </button>
              </section>
            )}

            {(problem || notice) && (
              <div className={`notice ${problem ? "error" : "success"}`} role="status">
                {problem ?? notice}
              </div>
            )}

            <button
              className="primary start-button"
              disabled={
                submitting ||
                Boolean(activeJob) ||
                !gpuReady ||
                !rightsConfirmed ||
                !mediaFile || Boolean(subtitleFile && sourceLanguage === "auto")
              }
            >
              <span>{
                submitting
                  ? uploadProgress
                    ? uploadPhaseLabels[uploadProgress.phase]
                    : "Đang tạo job…"
                  : activeJob
                    ? "Máy xử lý đang bận"
                    : !gpuReady
                      ? apiConnected ? `${cpuMode ? "CPU" : "GPU"} chưa sẵn sàng` : "Đang kiểm tra máy xử lý"
                    : subtitleFile && sourceLanguage === "auto"
                      ? "Hãy chọn ngôn ngữ của SRT"
                      : "Bắt đầu thuyết minh"
              }</span>
              <span aria-hidden="true">→</span>
            </button>
          </form>
        </div>

        <aside className="queue panel" id="jobs">
          <div className="panel-heading queue-heading">
            <div>
              <span className="step-number">03</span>
              <h2>Tiến trình</h2>
            </div>
            <button className="icon-button" type="button" onClick={() => void refreshOverview()} aria-label="Làm mới tiến trình">
              ↻
            </button>
          </div>

          {!jobs.length ? (
            <div className="empty-state">
              <span>00:00</span>
              <strong>Chưa có job nào</strong>
              <p>Job mới sẽ xuất hiện tại đây và tự cập nhật mỗi 3 giây.</p>
            </div>
          ) : (
            <div className="job-list">
              {jobs.map((job) => {
                const progress = jobProgressView(job);
                const activeStage = stagePosition(job);
                const title = job.spec?.search_query || shortId(job.release_id);
                return (
                  <article
                    className={`job-card status-${job.status} ${selectedJob?.id === job.id ? "selected" : ""}`}
                    key={job.id}
                  >
                    <div className="job-topline">
                      <span className="job-status">{statusLabels[job.status] ?? job.status}</span>
                      <span className="job-percent">{progress.overallPercent.toFixed(1)}%</span>
                    </div>
                    <h3>{title}</h3>
                    <p className="job-id">JOB {shortId(job.id)} · {stageLabels[job.stage] ?? job.stage}</p>
                    <div
                      className="progress-track"
                      role="progressbar"
                      aria-label={`Tiến trình tổng ${progress.overallPercent.toFixed(1)} phần trăm`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Number(progress.overallPercent.toFixed(1))}
                    >
                      <i style={{ width: `${progress.overallPercent}%` }} />
                    </div>
                    <p className="job-live-metric">
                      <strong>{progress.stagePercent.toFixed(1)}%</strong>
                      <span>{progress.summary}</span>
                    </p>
                    <div className="stage-track" aria-label="Các công đoạn xử lý">
                      {stageOrder.map((stage, index) => {
                        const skipped = isSkippedTranscriptStage(job, stage);
                        const state = skipped ? "bỏ qua" : index < activeStage ? "hoàn tất" : index === activeStage ? "đang chạy" : "đang chờ";
                        return (
                          <span
                            key={stage}
                            className={skipped ? "skipped" : index < activeStage ? "done" : index === activeStage ? "current" : ""}
                            title={`${stageLabels[stage]} · ${state}`}
                            aria-current={!skipped && index === activeStage ? "step" : undefined}
                            aria-label={`${stageLabels[stage]}: ${state}`}
                          >
                            <i aria-hidden="true" />
                            <small>{stageLabels[stage]}</small>
                          </span>
                        );
                      })}
                    </div>
                    {job.error?.message && <p className="job-error">{job.error.message}</p>}
                    <div className="job-actions">
                      <button className="small-button" type="button" onClick={() => setSelectedJobId(job.id)}>
                        Chi tiết
                      </button>
                      {job.status === "completed" && (
                        <a className="small-button download" href={`/v1/jobs/${job.id}/artifacts/video`}>
                          Tải MP4
                        </a>
                      )}
                      {(job.status === "paused" || (job.status === "failed" && job.error?.retryable)) && (
                        <button
                          className="small-button"
                          type="button"
                          disabled={busyAction === `${job.id}:resume`}
                          onClick={() => void jobMutation(job, "resume")}
                        >
                          Tiếp tục
                        </button>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </aside>
      </section>

      <section className="panel detail-panel" id="settings">
        <div className="panel-heading">
          <div>
            <h2>Cài đặt</h2>
          </div>
          <span className={`status-chip ${openaiKeyConfigured ? "online" : "offline"}`}>
            {openaiKeyConfigured
              ? "Key OpenAI đã cấu hình"
              : openaiKeyConfigured === false
                ? "Chưa có key OpenAI"
                : "Chưa rõ trạng thái key"}
          </span>
        </div>
        <p className="section-intro">
          Key OpenAI chỉ lưu phía máy chủ và không bao giờ hiển thị lại trên dashboard.
          Backend dịch và model API dưới đây là mặc định cho job mới tạo sau đó; bạn vẫn đổi được từng job trong form tạo job.
        </p>
        {(settingsProblem || settingsNotice) && (
          <div className={`notice ${settingsProblem ? "error" : "success"}`} role="status">
            {settingsProblem ?? settingsNotice}
          </div>
        )}
        <form className="admin-form" onSubmit={saveOpenAiKey}>
          <label className="field">
            <span>Key OpenAI</span>
            <input
              type="password"
              autoComplete="off"
              value={openaiKeyInput}
              onChange={(event) => setOpenaiKeyInput(event.target.value)}
              placeholder={openaiKeyConfigured ? "Đã lưu — nhập key mới để thay" : "sk-..."}
            />
          </label>
          <div className="integration-actions">
            <button
              className="primary"
              disabled={settingsBusy === "openai-key" || !openaiKeyInput.trim()}
            >
              {settingsBusy === "openai-key" ? "Đang lưu…" : "Lưu key"}
            </button>
            {openaiKeyConfigured && (
              <button
                className="small-button danger"
                type="button"
                disabled={settingsBusy === "openai-key-delete"}
                onClick={() => void removeOpenAiKey()}
              >
                Xóa key
              </button>
            )}
            <button
              className="small-button"
              type="button"
              onClick={() => void refreshOpenAiKey()}
            >
              Kiểm tra lại
            </button>
          </div>
        </form>
        <div className="settings-grid settings-defaults">
          <label className="field">
            <span>Backend dịch mặc định cho job mới</span>
            <select
              value={translationBackend}
              onChange={(event) =>
                setTranslationBackend(event.target.value === "openai" ? "openai" : "local")
              }
            >
              <option value="local">Cục bộ (Gemma trên máy)</option>
              <option value="openai">API OpenAI</option>
            </select>
          </label>
          <label className="field">
            <span>Model API mặc định</span>
            <input
              value={openaiModel}
              disabled={translationBackend !== "openai"}
              onChange={(event) => setOpenaiModel(event.target.value)}
              placeholder="gpt-4o-mini (để trống = mặc định)"
            />
          </label>
        </div>
        <p className="helper-copy">
          Khi chọn API OpenAI, lời thoại được gửi lên OpenAI để dịch; tiến trình dịch chạy ngoài worker offline và job sẽ hiện trạng thái “Đang dịch” cho tới khi xong.
        </p>
      </section>

      {selectedJob && (
        <section className="panel detail-panel" aria-labelledby="job-detail-title">
          <div className="panel-heading">
            <div>
              <span className="step-number">04</span>
              <h2 id="job-detail-title">Điều khiển job</h2>
            </div>
            <span className={`status-chip status-${selectedJob.status}`}>
              {statusLabels[selectedJob.status] ?? selectedJob.status}
            </span>
          </div>

          <div className="detail-meta-grid">
            <div><span>Job ID</span><strong>{selectedJob.id}</strong></div>
            <div><span>Công đoạn</span><strong>{stageLabels[selectedJob.stage] ?? selectedJob.stage}</strong></div>
            <div><span>Cập nhật</span><strong>{formatDate(selectedJob.updated_at)}</strong></div>
            <div><span>Ngôn ngữ</span><strong>{selectedJob.spec?.source_language ?? "auto"}</strong></div>
            <div>
              <span>Nhịp lời</span>
              <strong>{selectedJob.spec?.timing_profile === "natural" ? "Tự nhiên" : "Khớp chặt"}</strong>
            </div>
          </div>

          {selectedProgress && (
            <div className="progress-overview">
              <div className="progress-overview-head">
                <div>
                  <span className="progress-kicker">TIẾN TRÌNH TOÀN BỘ PIPELINE</span>
                  <strong>{selectedProgress.overallPercent.toFixed(1)}%</strong>
                  <p aria-live="polite">{selectedProgress.summary}</p>
                </div>
                <span className={`stream-indicator ${progressStreamState}`}>
                  <i aria-hidden="true" />
                  {progressStreamState === "live"
                    ? "Cập nhật trực tiếp"
                    : progressStreamState === "connecting"
                      ? "Đang nối luồng sự kiện"
                      : progressStreamState === "fallback"
                        ? "Dự phòng cập nhật 3 giây"
                        : selectedJob.status === "completed"
                          ? "Đã chốt tiến trình"
                          : selectedJob.status === "failed"
                            ? "Đã dừng do lỗi"
                            : selectedJob.status === "cancelled"
                              ? "Đã hủy tiến trình"
                          : "Theo dõi định kỳ"}
                </span>
              </div>

              <div
                className="progress-track progress-track-large"
                role="progressbar"
                aria-label={`Tiến trình tổng ${selectedProgress.overallPercent.toFixed(1)} phần trăm`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Number(selectedProgress.overallPercent.toFixed(1))}
              >
                <i style={{ width: `${selectedProgress.overallPercent}%` }} />
              </div>

              <div className="stage-progress-row">
                <div className="stage-progress-copy">
                  <span>Công đoạn {stageLabels[selectedJob.stage] ?? selectedJob.stage}</span>
                  <strong>{selectedProgress.stagePercent.toFixed(1)}%</strong>
                </div>
                <div
                  className="stage-progress-track"
                  role="progressbar"
                  aria-label={`Tiến trình công đoạn ${stageLabels[selectedJob.stage] ?? selectedJob.stage}`}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={Number(selectedProgress.stagePercent.toFixed(1))}
                >
                  <i style={{ width: `${selectedProgress.stagePercent}%` }} />
                </div>
              </div>

              <div className="progress-stat-grid">
                {selectedProgress.metrics.map((metric) => (
                  <div className="progress-stat" key={`${metric.label}-${metric.value}`}>
                    <span>{metric.label}</span>
                    <strong>{metric.value}</strong>
                    {metric.hint && <small>{metric.hint}</small>}
                  </div>
                ))}
                <div className="progress-stat">
                  <span>Đã chạy</span>
                  <strong>{formatElapsed(selectedJob.created_at, selectedJob.updated_at, selectedJob.status, nowMs)}</strong>
                  <small>Từ {formatDate(selectedJob.created_at)}</small>
                </div>
                <div className="progress-stat">
                  <span>Cập nhật gần nhất</span>
                  <strong>{formatFreshness(selectedJob.updated_at, nowMs)}</strong>
                  <small>{formatDate(selectedJob.updated_at)}</small>
                </div>
              </div>
            </div>
          )}

          <div className="detail-stage-track" aria-label="Trạng thái từng công đoạn">
            {stageOrder.map((stage, index) => {
              const activeStage = stagePosition(selectedJob);
              const skipped = isSkippedTranscriptStage(selectedJob, stage);
              const current = !skipped && index === activeStage;
              const attention = current && ["failed", "paused", "cancelling", "cancelled"].includes(selectedJob.status);
              const state = skipped
                ? "Bỏ qua"
                : index < activeStage
                  ? "Hoàn tất"
                  : current
                    ? selectedJob.status === "failed"
                      ? "Có lỗi"
                      : selectedJob.status === "paused"
                        ? "Tạm dừng"
                        : selectedJob.status === "cancelling"
                          ? "Đang hủy"
                          : selectedJob.status === "cancelled"
                            ? "Đã hủy"
                            : "Đang chạy"
                    : "Đang chờ";
              return (
                <div
                  key={stage}
                  className={`${skipped ? "skipped" : index < activeStage ? "done" : current ? "current" : ""} ${attention ? "attention" : ""}`}
                  aria-current={current ? "step" : undefined}
                  aria-label={`${stageLabels[stage]}: ${state}`}
                >
                  <i aria-hidden="true">
                    {skipped ? "—" : index < activeStage ? "✓" : attention ? "!" : String(index + 1).padStart(2, "0")}
                  </i>
                  <span>{stageLabels[stage]}</span>
                  <small>{state}</small>
                </div>
              );
            })}
          </div>

          {selectedJob.status === "needs_language" && (
            <div className="action-panel warning-panel">
              <div>
                <span className="action-kicker">CẦN BẠN QUYẾT ĐỊNH</span>
                <h3>Chọn ngôn ngữ chính của lời thoại</h3>
                <p>Whisper chưa đủ chắc chắn. Chọn đúng ngôn ngữ để job tiếp tục mà không chạy lại phần tải nguồn.</p>
              </div>
              {languageCandidates.length > 0 && (
                <div className="candidate-chips">
                  {languageCandidates.map((candidate, index) => {
                    const code = candidate.language ?? "";
                    const confidence = candidate.probability ?? candidate.confidence;
                    return (
                      <button
                        type="button"
                        key={`${code}-${index}`}
                        disabled={!code || busyAction === `${selectedJob.id}:language`}
                        onClick={() => void selectLanguage(selectedJob, code)}
                      >
                        <strong>{code || "?"}</strong>
                        <small>{typeof confidence === "number" ? `${(confidence * 100).toFixed(0)}%` : "đề xuất"}</small>
                      </button>
                    );
                  })}
                </div>
              )}
              <div className="inline-choice">
                <label className="field">
                  <span>Chọn thủ công</span>
                  <select value={languageChoice} onChange={(event) => setLanguageChoice(event.target.value)}>
                    {languageOptions.filter(([value]) => value !== "auto").map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
                <button
                  className="primary"
                  type="button"
                  disabled={busyAction === `${selectedJob.id}:language`}
                  onClick={() => void selectLanguage(selectedJob, languageChoice)}
                >
                  Xác nhận ngôn ngữ
                </button>
              </div>
            </div>
          )}

          {selectedJob.status === "needs_subtitle_selection" && (
            <div className="action-panel warning-panel">
              <div>
                <span className="action-kicker">CẦN BẠN QUYẾT ĐỊNH</span>
                <h3>Chọn phụ đề hoặc dùng ASR</h3>
                <p>Không có ứng viên nào đủ chắc chắn để tự chọn. Hãy kiểm tra tên release, ngôn ngữ và điểm khớp.</p>
              </div>
              <div className="subtitle-candidates">
                {subtitleCandidates.map((candidate) => (
                  <article key={candidate.subtitle_id}>
                    <div>
                      <strong>{candidate.release_name || candidate.subtitle_id}</strong>
                      <p>
                        {candidate.language ?? "?"} · {candidate.format ?? "?"} · nguồn {candidate.source ?? "?"}
                        {typeof candidate.score === "number" ? ` · điểm ${candidate.score.toFixed(2)}` : ""}
                      </p>
                      <small>
                        {candidate.hearing_impaired ? "Có mô tả âm thanh · " : ""}
                        {candidate.forced ? "Forced · " : ""}
                        {Array.isArray(candidate.matched_by) ? candidate.matched_by.join(", ") : candidate.matched_by}
                      </small>
                    </div>
                    <button
                      className="small-button download"
                      type="button"
                      disabled={busyAction === `${selectedJob.id}:subtitle`}
                      onClick={() => void selectSubtitle(selectedJob, candidate.subtitle_id)}
                    >
                      Dùng phụ đề này
                    </button>
                  </article>
                ))}
                {!subtitleCandidates.length && <p className="helper-copy">API chưa trả về ứng viên phụ đề.</p>}
              </div>
              <button
                className="small-button"
                type="button"
                disabled={busyAction === `${selectedJob.id}:subtitle`}
                onClick={() => void selectSubtitle(selectedJob)}
              >
                Bỏ qua phụ đề, dùng ASR
              </button>
            </div>
          )}

          {warnings.length > 0 && (
            <div className="warning-list">
              <strong>Cảnh báo chất lượng</strong>
              <ul>{warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul>
            </div>
          )}

          {selectedJob.error?.message && (
            <div className="notice error">
              <strong>{selectedJob.error.code ?? "pipeline_error"}</strong>: {selectedJob.error.message}
              {selectedJob.error.retryable === false ? " · Lỗi này không thể thử lại tự động." : ""}
            </div>
          )}

          <div className="detail-actions">
            <button
              className="small-button"
              type="button"
              disabled={busyAction === `${selectedJob.id}:refresh`}
              onClick={() => void jobMutation(selectedJob, "refresh")}
            >
              Làm mới job
            </button>
            {(selectedJob.status === "paused" || (selectedJob.status === "failed" && selectedJob.error?.retryable)) && (
              <button
                className="small-button download"
                type="button"
                disabled={busyAction === `${selectedJob.id}:resume`}
                onClick={() => void jobMutation(selectedJob, "resume")}
              >
                Tiếp tục từ checkpoint
              </button>
            )}
            {!["completed", "cancelling", "cancelled"].includes(selectedJob.status) && (
              <button
                className="small-button danger"
                type="button"
                disabled={busyAction === `${selectedJob.id}:cancel`}
                onClick={() => void jobMutation(selectedJob, "cancel")}
              >
                Hủy job
              </button>
            )}
          </div>

          {selectedJob.status === "ready_translation" && selectedJob.spec?.translation_backend === "openai" && (
            <div className="action-panel">
              <div>
                <span className="action-kicker">DỊCH BẰNG API</span>
                <h3>Dịch lời thoại qua OpenAI</h3>
                <p>
                  Job này đã cấu hình dịch bằng API
                  {selectedJob.spec?.models?.translation ? ` (model ${selectedJob.spec.models.translation})` : ""}
                  . Bấm để bắt đầu dịch; worker offline không tự gọi API nên bước này chạy qua tiến trình dịch riêng.
                </p>
              </div>
              {translateKeyMissing && (
                <div className="notice error" role="status">
                  Chưa có key OpenAI trên máy chủ. <a href="#settings">Mở Cài đặt để nhập key</a> rồi thử lại.
                </div>
              )}
              <div className="detail-actions">
                <button
                  className="small-button download"
                  type="button"
                  disabled={busyAction === `${selectedJob.id}:translate`}
                  onClick={() => void translateWithApi(selectedJob)}
                >
                  {busyAction === `${selectedJob.id}:translate` ? "Đang gửi…" : "Dịch bằng API"}
                </button>
              </div>
            </div>
          )}

          {selectedJob.status === "translating" && (
            <div className="notice" role="status">
              Đang dịch bằng API — trang tự cập nhật; khi dịch xong job chuyển sang “Sẵn sàng tạo giọng” và Phase 4 chạy tiếp tự động.
            </div>
          )}

          {selectedJob.status === "completed" && (
            <div className="artifact-panel">
              <div>
                <span className="action-kicker">ĐẦU RA ĐÃ XÁC MINH</span>
                <h3>Tải kết quả</h3>
                <p>MP4 thuyết minh, phụ đề tiếng Việt và báo cáo timing được phục vụ trực tiếp từ máy GPU.</p>
              </div>
              <div className="artifact-actions">
                <a href={`/v1/jobs/${selectedJob.id}/artifacts/video`}>MP4 thuyết minh</a>
                <a href={`/v1/jobs/${selectedJob.id}/artifacts/subtitle`}>Phụ đề SRT</a>
                <a href={`/v1/jobs/${selectedJob.id}/artifacts/timing`}>Timing JSON</a>
              </div>
            </div>
          )}
        </section>
      )}

      {selectedJob && (
        <section className="panel studio-panel" aria-labelledby="studio-title">
          <div className="panel-heading">
            <div>
              <h2 id="studio-title">Studio lồng tiếng</h2>
            </div>
            <button
              className="small-button"
              type="button"
              onClick={() => void refreshStudio(selectedJob.id)}
            >
              Tải lại studio
            </button>
          </div>
          <p className="section-intro">
            Bảng cast, kịch bản và thông số mix của job này.
            {studio ? ` Backend TTS: ${studio.tts_backend}.` : ""}
          </p>
          {studio?.qc_summary && (
            <p className="helper-copy">
              AI nghe lại: {studio.qc_summary.ok ?? 0}/{studio.qc_summary.total ?? 0} câu khớp kịch bản
              {studio.qc_summary.needs_attention
                ? `, ${studio.qc_summary.needs_attention} câu cần chú ý (sai nhiều: ${studio.qc_summary.bad ?? 0}, không nghe thấy: ${studio.qc_summary.silent ?? 0}, nên nghe lại: ${studio.qc_summary.review ?? 0}).`
                : ". Mọi câu đều khớp."}
            </p>
          )}

          {studioError && (
            <div className="notice error" role="status">{studioError}</div>
          )}
          {studioNotice && (
            <div className="notice" role="status">{studioNotice}</div>
          )}
          {!studio && !studioError && <p className="helper-copy">Đang tải studio…</p>}

          {studio && !studio.editable && (
            <div className="notice" role="status">
              Studio mở khi job đã hoàn tất hoặc đã dịch xong, đang chờ thuyết minh
            </div>
          )}

          {studio && studio.editable && (
            <>
              <div>
                <span className="action-kicker">BẢNG CAST</span>
                <h3>Nhân vật và giọng đọc</h3>
                <p className="helper-copy">
                  Đổi tên hiển thị hoặc giọng rồi bấm Lưu cast để gửi toàn bộ bảng một lần.
                </p>
              </div>
              {studio.cast.length === 0 && (
                <p className="helper-copy">Job này chưa có nhân vật nào.</p>
              )}
              <div className="studio-cast">
                {studio.cast.map((member) => {
                  const draft = castDraft[member.speaker] ?? {
                    display_name: member.display_name ?? "",
                    voice_id: member.voice_id ?? "",
                    reference_path: member.reference_path ?? "",
                  };
                  return (
                    <article key={member.speaker} className="studio-cast-row">
                      <div className="studio-cast-name">
                        <strong>{member.display_name || member.speaker}</strong>
                        <small>
                          Nhãn gốc: {member.speaker}
                          {typeof member.line_count === "number" ? ` · ${member.line_count} câu` : ""}
                          {typeof member.first_start_us === "number"
                            ? ` · bắt đầu ${formatMmSs(member.first_start_us)}`
                            : ""}
                        </small>
                      </div>
                      <label className="field">
                        <span>Tên hiển thị</span>
                        <input
                          value={draft.display_name}
                          onChange={(event) =>
                            setCastDraft((current) => ({
                              ...current,
                              [member.speaker]: { ...draft, display_name: event.target.value },
                            }))
                          }
                          placeholder={member.speaker}
                        />
                      </label>
                      <label className="field">
                        <span>Voice ID (Piper)</span>
                        <input
                          value={draft.voice_id}
                          onChange={(event) =>
                            setCastDraft((current) => ({
                              ...current,
                              [member.speaker]: { ...draft, voice_id: event.target.value },
                            }))
                          }
                          placeholder="VD: 3"
                        />
                      </label>
                      <label className="field">
                        <span>File giọng mẫu</span>
                        <input
                          value={draft.reference_path}
                          onChange={(event) =>
                            setCastDraft((current) => ({
                              ...current,
                              [member.speaker]: { ...draft, reference_path: event.target.value },
                            }))
                          }
                          placeholder="/path/voice.wav"
                        />
                      </label>
                    </article>
                  );
                })}
              </div>
              {studio.cast.length > 0 && (
                <div className="detail-actions">
                  <button
                    className="small-button download"
                    type="button"
                    disabled={studioBusy === "cast"}
                    onClick={() => void saveStudioCast(selectedJob.id)}
                  >
                    {studioBusy === "cast" ? "Đang lưu…" : "Lưu cast"}
                  </button>
                </div>
              )}

              <div>
                <span className="action-kicker">KỊCH BẢN</span>
                <h3>Câu thoại</h3>
              </div>
              {studio.script.length === 0 && (
                <p className="helper-copy">Job này chưa có câu thoại nào.</p>
              )}
              <div className="studio-script">
                {studio.script.map((line) => {
                  const text = scriptDraft[line.ordinal] ?? line.text;
                  const busyLine = studioBusy === `line:${line.ordinal}`;
                  const busyRetake = studioBusy === `retake:${line.ordinal}`;
                  return (
                    <article key={line.ordinal} className="studio-line">
                      <div className="studio-line-meta">
                        <strong>Câu {line.ordinal + 1}</strong>
                        <span>{formatMmSs(line.start_us)}</span>
                        <span>{line.display_name || line.speaker || "Không rõ nhân vật"}</span>
                        {line.voice_id && <small>Giọng: {line.voice_id}</small>}
                        {line.synthesized && <small>Đã thu</small>}
                        {line.qc_status && line.qc_status !== "ok" && (
                          <small>
                            AI nghe: {line.qc_status === "silent"
                              ? "không nghe thấy gì"
                              : line.qc_status === "bad"
                                ? "sai nhiều, cần thu lại"
                                : line.qc_status === "review"
                                  ? "nên nghe lại"
                                  : "lệch nhẹ"}
                            {line.qc_wer != null && ` (WER ${(line.qc_wer * 100).toFixed(0)}%)`}
                            {line.qc_heard ? ` — nghe ra: “${line.qc_heard}”` : ""}
                          </small>
                        )}
                      </div>
                      <label className="field grow">
                        <span>Câu thoại</span>
                        <textarea
                          rows={2}
                          value={text}
                          onChange={(event) =>
                            setScriptDraft((current) => ({
                              ...current,
                              [line.ordinal]: event.target.value,
                            }))
                          }
                        />
                      </label>
                      <div className="studio-line-actions">
                        <button
                          className="small-button"
                          type="button"
                          disabled={busyLine}
                          onClick={() => void saveStudioLine(selectedJob.id, line)}
                        >
                          {busyLine ? "Đang lưu…" : "Lưu câu"}
                        </button>
                        <button
                          className="small-button download"
                          type="button"
                          disabled={busyRetake}
                          onClick={() => void retakeStudioLines(selectedJob.id, [line.ordinal])}
                        >
                          {busyRetake ? "Đang gửi…" : "Thu lại câu"}
                        </button>
                      </div>
                      {line.synthesized && (
                        <audio
                          controls
                          src={`/v1/jobs/${encodeURIComponent(selectedJob.id)}/studio/lines/${line.ordinal}/audio`}
                        />
                      )}
                    </article>
                  );
                })}
              </div>

              <div>
                <span className="action-kicker">PHÒNG MIX</span>
                <h3>Thông số mix cuối</h3>
              </div>
              <div className="studio-mix">
                <label className="field">
                  <span>Lời thoại (LUFS)</span>
                  <input
                    type="number"
                    step="any"
                    value={mixDraft.narration_lufs}
                    onChange={(event) =>
                      setMixDraft((current) => ({ ...current, narration_lufs: event.target.value }))
                    }
                  />
                </label>
                <label className="field">
                  <span>Nhạc nền (LUFS)</span>
                  <input
                    type="number"
                    step="any"
                    value={mixDraft.accompaniment_lufs}
                    onChange={(event) =>
                      setMixDraft((current) => ({ ...current, accompaniment_lufs: event.target.value }))
                    }
                  />
                </label>
                <label className="field">
                  <span>Tỉ lệ ducking</span>
                  <input
                    type="number"
                    step="any"
                    value={mixDraft.ducking_ratio}
                    onChange={(event) =>
                      setMixDraft((current) => ({ ...current, ducking_ratio: event.target.value }))
                    }
                  />
                </label>
                <label className="field">
                  <span>Ngưỡng ducking</span>
                  <input
                    type="number"
                    step="any"
                    value={mixDraft.ducking_threshold}
                    onChange={(event) =>
                      setMixDraft((current) => ({ ...current, ducking_threshold: event.target.value }))
                    }
                  />
                </label>
                <label className="field">
                  <span>True peak (dB)</span>
                  <input
                    type="number"
                    step="any"
                    value={mixDraft.true_peak_db}
                    onChange={(event) =>
                      setMixDraft((current) => ({ ...current, true_peak_db: event.target.value }))
                    }
                  />
                </label>
              </div>
              <div className="detail-actions">
                <button
                  className="small-button download"
                  type="button"
                  disabled={studioBusy === "mix"}
                  onClick={() => void saveStudioMix(selectedJob.id)}
                >
                  {studioBusy === "mix" ? "Đang lưu…" : "Lưu mix"}
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {selectedJob && selectedJob.status === "awaiting_approval" && (
        <section className="panel detail-panel" aria-labelledby="review-title">
          <div className="panel-heading">
            <div>
              <h2 id="review-title">Duyệt kết quả</h2>
            </div>
            <button
              className="small-button"
              type="button"
              onClick={() => void refreshReview(selectedJob.id)}
            >
              Tải lại bản duyệt
            </button>
          </div>
          <p className="section-intro">
            Xem video đã dựng xong trước khi xuất file cuối. Bấm “Duyệt và xuất file” để chốt, hoặc “Cần sửa” để job quay lại Studio sửa rồi dựng lại.
          </p>
          {reviewError && (
            <div className="notice error" role="status">{reviewError}</div>
          )}
          {reviewNotice && (
            <div className="notice" role="status">{reviewNotice}</div>
          )}
          {!review && !reviewError && <p className="helper-copy">Đang tải bản duyệt…</p>}
          {review && (
            <>
              <video
                className="review-video"
                controls
                src={review.preview_video_url || `/v1/jobs/${encodeURIComponent(selectedJob.id)}/review/video`}
              />
              <div className="detail-actions">
                <a
                  className="small-button download"
                  href={review.preview_subtitle_url || `/v1/jobs/${encodeURIComponent(selectedJob.id)}/review/subtitle`}
                >
                  Tải phụ đề duyệt
                </a>
              </div>
              {review.qc_summary && (
                <p className="helper-copy">
                  AI nghe lại: {review.qc_summary.ok ?? 0}/{review.qc_summary.total ?? 0} câu khớp kịch bản
                  {review.qc_summary.needs_attention
                    ? `, ${review.qc_summary.needs_attention} câu cần chú ý (sai nhiều: ${review.qc_summary.bad ?? 0}, không nghe thấy: ${review.qc_summary.silent ?? 0}, nên nghe lại: ${review.qc_summary.review ?? 0}).`
                    : ". Mọi câu đều khớp."}
                </p>
              )}
              <div className="studio-script">
                {review.script.map((line) => (
                  <article key={line.ordinal} className="studio-line">
                    <div className="studio-line-meta">
                      <strong>Câu {line.ordinal + 1}</strong>
                      <span>{formatMmSs(line.start_us)}</span>
                      <span>{line.display_name || line.speaker || "Không rõ nhân vật"}</span>
                      {line.qc_status && line.qc_status !== "ok" && (
                        <small>
                          AI nghe: {line.qc_status === "silent"
                            ? "không nghe thấy gì"
                            : line.qc_status === "bad"
                              ? "sai nhiều, cần thu lại"
                              : line.qc_status === "review"
                                ? "nên nghe lại"
                                : "lệch nhẹ"}
                          {line.qc_wer != null && ` (WER ${(line.qc_wer * 100).toFixed(0)}%)`}
                          {line.qc_heard ? ` — nghe ra: “${line.qc_heard}”` : ""}
                        </small>
                      )}
                    </div>
                    <p className="review-line-text">{line.text}</p>
                  </article>
                ))}
                {review.script.length === 0 && (
                  <p className="helper-copy">Bản duyệt chưa có câu thoại nào.</p>
                )}
              </div>
              <label className="field">
                <span>Ghi chú cần sửa (tùy chọn, chỉ dùng khi bấm “Cần sửa”)</span>
                <textarea
                  rows={2}
                  value={rejectNote}
                  onChange={(event) => setRejectNote(event.target.value)}
                  placeholder="VD: Câu 5 sai tên nhân vật, câu 12 ngắt nhịp sớm"
                />
              </label>
              <div className="detail-actions">
                <button
                  className="small-button download"
                  type="button"
                  disabled={reviewBusy !== null}
                  onClick={() => void approveReview(selectedJob.id)}
                >
                  {reviewBusy === "approve" ? "Đang duyệt…" : "Duyệt và xuất file"}
                </button>
                <button
                  className="small-button danger"
                  type="button"
                  disabled={reviewBusy !== null}
                  onClick={() => void rejectReview(selectedJob.id)}
                >
                  {reviewBusy === "reject" ? "Đang gửi…" : "Cần sửa"}
                </button>
              </div>
            </>
          )}
        </section>
      )}

      <section className="panel inventory-panel" id="models">
        <div className="panel-heading">
          <div>
            <span className="step-number">05</span>
            <h2>Kho model cục bộ</h2>
          </div>
          <button className="small-button" type="button" onClick={() => void refreshCatalog()}>Làm mới danh mục</button>
        </div>
        <p className="section-intro">
          Dashboard chỉ chọn model đã cài và xác minh; việc tải model là thao tác quản trị rõ ràng qua CLI, không xảy ra trong lúc xử lý job.
        </p>
        <div className="model-catalog">
          {catalog.models.map((model) => (
            <article key={model.id} className={model.installed && model.valid ? "ready" : "missing"}>
              <div className="model-title-row">
                <span>{stageLabels[model.stage] ?? model.stage}</span>
                <i>{model.installed && model.valid ? "SẴN SÀNG" : model.installed ? "CHƯA XÁC MINH" : "CHƯA CÀI"}</i>
              </div>
              <h3>{model.id}</h3>
              <p>{model.backend} · {model.compute_type ?? "compute mặc định"}</p>
              <div>
                <small>{model.quality_tier ?? "standard"}</small>
                <small>{model.minimum_vram_mib ? `${Math.ceil(model.minimum_vram_mib / 1024)} GB VRAM` : "CPU"}</small>
                <small>{model.license}</small>
              </div>
            </article>
          ))}
          {!catalog.models.length && <p className="helper-copy">Danh mục model chưa sẵn sàng.</p>}
        </div>
      </section>

      <section className="panel integrations-panel" id="integrations">
        <div className="panel-heading">
          <div>
            <span className="step-number">06</span>
            <h2>Phụ đề trực tuyến</h2>
          </div>
          <button className="small-button" type="button" onClick={() => void refreshIntegrations()}>Làm mới tích hợp</button>
        </div>
        <p className="section-intro">
          Bí mật được kiểm tra và lưu phía máy chủ; dashboard không đọc lại hoặc hiển thị API key, token hay mật khẩu.
        </p>

        {(integrationProblem || integrationNotice) && (
          <div className={`notice ${integrationProblem ? "error" : "success"}`} role="status">
            {integrationProblem ?? integrationNotice}
          </div>
        )}

        <div className="integration-grid">
          <article className="integration-card">
            <div className="integration-heading">
              <div>
                <span className="action-kicker">SUBTITLE PROVIDER</span>
                <h3>OpenSubtitles</h3>
              </div>
              <span className={`status-chip ${openSubStatus?.configured ? "online" : "offline"}`}>
                {openSubStatus?.cleanup_pending
                  ? "Cần dọn secret cũ"
                  : openSubStatus?.configured
                    ? "Đã cấu hình"
                    : "Chưa cấu hình"}
              </span>
            </div>
            <p>
              Nhập API key và tài khoản để máy chủ đăng nhập, kiểm tra token rồi ghi các file secret với quyền hạn chế. Mật khẩu không được lưu.
            </p>
            {openSubStatus?.cleanup_pending ? (
              <div className="readonly-note">
                {openSubStatus.can_delete === false
                  ? "Cấu hình đã bị vô hiệu hóa nhưng máy chủ hiện không thể dọn file còn sót. Hãy sửa quyền thư mục secret trên host rồi tải lại trang."
                  : "Cấu hình đã bị vô hiệu hóa nhưng còn file dọn dở. Bấm “Dọn cấu hình còn sót” để hoàn tất trước khi đăng nhập lại."}
              </div>
            ) : openSubStatus?.can_manage === false ? (
              <div className="readonly-note">
                Cấu hình đang ở chế độ chỉ đọc (thường do secret được mount từ Docker). Hãy sửa secret ở máy host rồi khởi động lại stack.
              </div>
            ) : null}
            <form className="admin-form" onSubmit={saveOpenSubtitles}>
              <label className="field">
                <span>API key</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={openSubApiKey}
                  onChange={(event) => setOpenSubApiKey(event.target.value)}
                  placeholder={openSubStatus?.configured ? "Đã lưu — nhập key mới để thay" : "OpenSubtitles API key"}
                />
              </label>
              <label className="field">
                <span>Tên đăng nhập</span>
                <input
                  autoComplete="username"
                  value={openSubUsername}
                  onChange={(event) => setOpenSubUsername(event.target.value)}
                  placeholder="Tên tài khoản"
                />
              </label>
              <label className="field">
                <span>Mật khẩu dùng một lần để lấy token</span>
                <input
                  type="password"
                  autoComplete="current-password"
                  value={openSubPassword}
                  onChange={(event) => setOpenSubPassword(event.target.value)}
                  placeholder="Không được lưu trên server"
                />
              </label>
              <div className="integration-actions">
                <button
                  className="primary"
                  disabled={
                    integrationBusy === "opensubtitles" ||
                    openSubStatus?.can_manage === false ||
                    openSubStatus?.cleanup_pending === true
                  }
                >
                  {integrationBusy === "opensubtitles" ? "Đang xác thực…" : "Xác thực và lưu"}
                </button>
                {(openSubStatus?.configured || openSubStatus?.cleanup_pending) &&
                  openSubStatus?.can_delete !== false && (
                  <button
                    className="small-button danger"
                    type="button"
                    disabled={integrationBusy === "opensubtitles-delete"}
                    onClick={() => void removeOpenSubtitles()}
                  >
                    {openSubStatus?.cleanup_pending ? "Dọn cấu hình còn sót" : "Xóa cấu hình"}
                  </button>
                )}
              </div>
            </form>
            <p className="tunnel-note">
              Sau khi đổi cấu hình, chạy <code>dub stack restart</code> nếu API báo cần khởi động lại. Token hết hạn có thể được cấp lại bằng form này.
            </p>
          </article>
        </div>
      </section>

      <footer>
        <span>LỒNG TIẾNG GPU · LOCAL-FIRST</span>
        <span>Dữ liệu media và bí mật ở lại trên máy xử lý</span>
      </footer>
    </main>
  );
}
