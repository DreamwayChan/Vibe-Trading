import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Loader2,
  Play,
  PlugZap,
  QrCode,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  Square,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  api,
  type ChannelAdapterStatus,
  type ChannelAuthSession,
  type ChannelLogLine,
  type ChannelRuntimeStatus,
  type ChannelSettings,
  type PendingPairing,
} from "@/lib/api";

const buttonBase =
  "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50";
const primaryButton = `${buttonBase} border-primary/30 bg-primary/10 text-primary hover:bg-primary/15`;
const ghostButton = `${buttonBase} bg-background text-muted-foreground hover:bg-muted/60`;
const dangerButton = `${buttonBase} border-red-200 bg-red-50 text-red-600 hover:bg-red-100`;

const fieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20";

type StateKey =
  | "stateRunning"
  | "stateStarting"
  | "stateNeedsAuth"
  | "stateError"
  | "stateStopped"
  | "unavailable"
  | "disabled";

interface StateMeta {
  key: StateKey;
  className: string;
}

const STATE_META: Record<string, StateMeta> = {
  running: { key: "stateRunning", className: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  starting: { key: "stateStarting", className: "bg-blue-50 text-blue-700 border-blue-200" },
  needs_auth: { key: "stateNeedsAuth", className: "bg-amber-50 text-amber-700 border-amber-200" },
  error: { key: "stateError", className: "bg-red-50 text-red-700 border-red-200" },
  unavailable: { key: "unavailable", className: "bg-muted text-muted-foreground border-transparent" },
  stopped: { key: "stateStopped", className: "bg-muted/70 text-muted-foreground border-transparent" },
  disabled: { key: "disabled", className: "bg-muted/40 text-muted-foreground border-transparent" },
};

interface CredentialField {
  key: string;
  label: string;
  secret?: boolean;
}

/**
 * Per-platform credential fields. Channels outside this map can still be
 * enabled/disabled here; their raw section stays editable in agent.json.
 */
const CREDENTIAL_FIELDS: Record<string, CredentialField[]> = {
  telegram: [{ key: "token", label: "Bot token", secret: true }],
  discord: [{ key: "token", label: "Bot token", secret: true }],
  slack: [
    { key: "bot_token", label: "Bot token (xoxb-…)", secret: true },
    { key: "app_token", label: "App token (xapp-…)", secret: true },
  ],
  wecom: [
    { key: "bot_id", label: "Bot ID" },
    { key: "secret", label: "Secret", secret: true },
  ],
  feishu: [
    { key: "app_id", label: "App ID" },
    { key: "app_secret", label: "App secret", secret: true },
  ],
  dingtalk: [
    { key: "client_id", label: "Client ID" },
    { key: "client_secret", label: "Client secret", secret: true },
  ],
  matrix: [
    { key: "homeserver", label: "Homeserver URL" },
    { key: "user_id", label: "User ID" },
    { key: "access_token", label: "Access token", secret: true },
  ],
  email: [
    { key: "address", label: "Email address" },
    { key: "password", label: "Password / app password", secret: true },
  ],
};

function stateOf(item: ChannelAdapterStatus): string {
  if (item.state) return item.state;
  if (!item.enabled) return "disabled";
  if (item.available === false) return "unavailable";
  if (item.running) return "running";
  return item.loaded ? "stopped" : "error";
}

interface ChannelsPanelProps {
  status: ChannelRuntimeStatus | null;
  refreshing: boolean;
  onRefresh: () => Promise<void> | void;
  onStatus: (status: ChannelRuntimeStatus) => void;
}

export function ChannelsPanel({ status, refreshing, onRefresh, onStatus }: ChannelsPanelProps) {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<ChannelSettings | null>(null);
  const [pending, setPending] = useState<PendingPairing[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [wizard, setWizard] = useState<{ name: string; session: ChannelAuthSession } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [logsFor, setLogsFor] = useState<string | null>(null);

  const loadSettings = useCallback(async () => {
    const [settingsResult, pendingResult] = await Promise.allSettled([
      api.getChannelSettings(),
      api.getPendingPairings(),
    ]);
    if (settingsResult.status === "fulfilled") setSettings(settingsResult.value ?? null);
    if (pendingResult.status === "fulfilled") setPending(pendingResult.value?.pending ?? []);
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const channels = useMemo(() => {
    const order: Record<string, number> = { needs_auth: 0, error: 1, running: 2, starting: 3, stopped: 4, unavailable: 5, disabled: 6 };
    return Object.entries(status?.channels ?? {}).sort(([, a], [, b]) => {
      const rank = (order[stateOf(a)] ?? 9) - (order[stateOf(b)] ?? 9);
      if (rank !== 0) return rank;
      return (a.display_name || a.name).localeCompare(b.display_name || b.name);
    });
  }, [status]);

  const runningCount = channels.filter(([, item]) => item.running).length;
  const needsAuthCount = channels.filter(([, item]) => stateOf(item) === "needs_auth").length;

  const apply = (next: ChannelRuntimeStatus) => onStatus(next);

  const runAll = async (action: "start" | "stop") => {
    setBusy(`all:${action}`);
    try {
      const next = action === "start" ? await api.startChannels() : await api.stopChannels();
      apply(next);
      toast.success(
        action === "start" ? t("settings.channels.started") : t("settings.channels.stoppedToast"),
      );
    } catch (error) {
      toast.error(
        `${action === "start" ? t("settings.channels.startFailed") : t("settings.channels.stopFailed")}: ${
          error instanceof Error ? error.message : "Unknown error"
        }`,
      );
    } finally {
      setBusy(null);
    }
  };

  const toggleAutoStart = async () => {
    if (!settings || settings.env_locked) return;
    setBusy("auto_start");
    try {
      const next = await api.updateChannelSettings({ auto_start: !settings.auto_start });
      setSettings(next);
      toast.success(
        next.auto_start
          ? t("settings.channels.autoStartOn")
          : t("settings.channels.autoStartOff"),
      );
    } catch (error) {
      toast.error(
        `${t("settings.channels.saveFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const channelAction = async (
    name: string,
    action: "start" | "stop" | "restart",
  ) => {
    setBusy(`${name}:${action}`);
    try {
      const next =
        action === "start"
          ? await api.startChannel(name)
          : action === "stop"
            ? await api.stopChannel(name)
            : await api.restartChannel(name);
      apply(next);
    } catch (error) {
      toast.error(
        `${name}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const toggleEnabled = async (item: ChannelAdapterStatus) => {
    setBusy(`${item.name}:toggle`);
    try {
      const next = await api.patchChannel(item.name, { enabled: !item.enabled });
      apply(next);
    } catch (error) {
      toast.error(
        `${t("settings.channels.saveFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const saveCredentials = async (name: string) => {
    setBusy(`${name}:save`);
    try {
      const next = await api.patchChannel(name, { enabled: true, ...credentials });
      apply(next);
      setCredentials({});
      setExpanded(null);
      toast.success(t("settings.channels.saved"));
    } catch (error) {
      toast.error(
        `${t("settings.channels.saveFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const beginBinding = async (name: string) => {
    setBusy(`${name}:bind`);
    try {
      const session = await api.startChannelAuth(name);
      setWizard({ name, session });
    } catch (error) {
      toast.error(
        `${t("settings.channels.bindFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const unbind = async (name: string) => {
    setBusy(`${name}:unbind`);
    try {
      const next = await api.unbindChannel(name);
      apply(next);
      toast.success(t("settings.channels.unbound"));
    } catch (error) {
      toast.error(
        `${t("settings.channels.unbindFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  const refreshAll = async () => {
    await onRefresh();
    await loadSettings();
  };

  const decidePairing = async (code: string, approve: boolean) => {
    setBusy(`pair:${code}`);
    try {
      if (approve) {
        const result = await api.approvePairing(code);
        toast.success(`${t("settings.channels.pairApproved")}: ${result.channel} · ${result.sender_id}`);
      } else {
        await api.denyPairing(code);
        toast.success(t("settings.channels.pairDenied"));
      }
      await loadSettings();
    } catch (error) {
      toast.error(
        `${t("settings.channels.pairFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-2">
          <PlugZap className="mt-0.5 h-4 w-4 text-primary" />
          <div>
            <h2 className="text-base font-semibold">{t("settings.channels.title")}</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              {t("settings.channels.description")}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={toggleAutoStart}
            disabled={!settings || settings.env_locked || busy === "auto_start"}
            aria-pressed={settings?.auto_start ?? false}
            className={settings?.auto_start ? primaryButton : ghostButton}
            title={
              settings?.env_locked
                ? t("settings.channels.envLocked")
                : t("settings.channels.autoStartHint")
            }
          >
            {settings?.auto_start ? <Check className="h-3.5 w-3.5" /> : <X className="h-3.5 w-3.5" />}
            {t("settings.channels.autoStart")}
          </button>
          <button type="button" onClick={refreshAll} disabled={refreshing} className={ghostButton}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            {t("settings.channels.refresh")}
          </button>
          <button
            type="button"
            onClick={() => void runAll("start")}
            disabled={!status || busy !== null}
            className={primaryButton}
          >
            {busy === "all:start" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {t("settings.channels.start")}
          </button>
          <button
            type="button"
            onClick={() => void runAll("stop")}
            disabled={!status || busy !== null}
            className={ghostButton}
          >
            {busy === "all:stop" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
            {t("settings.channels.stop")}
          </button>
        </div>
      </div>

      {!status ? (
        <div className="mt-4 rounded-md border bg-muted/20 px-4 py-6 text-center text-sm text-muted-foreground">
          {t("settings.channels.refreshFailed")}
        </div>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap gap-4 text-xs text-muted-foreground">
            <span>
              {t("settings.channels.runtime")}:{" "}
              <span className="font-medium text-foreground">
                {status.running ? t("settings.channels.running") : t("settings.channels.stopped")}
              </span>
            </span>
            <span>
              {t("settings.channels.active")}: <span className="font-medium text-foreground">{runningCount}</span>
            </span>
            <span>
              {t("settings.channels.enabled")}:{" "}
              <span className="font-medium text-foreground">
                {channels.filter(([, item]) => item.enabled).length}
              </span>
            </span>
            {needsAuthCount > 0 && (
              <span className="text-amber-600">
                {t("settings.channels.needsAuthCount")}:{" "}
                <span className="font-medium">{needsAuthCount}</span>
              </span>
            )}
          </div>

          <div className="mt-3 divide-y rounded-md border">
            {channels.map(([name, item]) => {
              const state = stateOf(item);
              const meta = STATE_META[state] ?? STATE_META.stopped;
              const isBusy = busy?.startsWith(`${name}:`) ?? false;
              const fields = CREDENTIAL_FIELDS[name];
              const canBind = (item.auth_kind ?? "credentials") === "qr";
              return (
                <div key={name} className="px-3 py-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <div className="min-w-[180px] flex-1">
                      <div className="text-sm font-medium">{item.display_name || name}</div>
                      <div className="text-xs text-muted-foreground">{name}</div>
                    </div>
                    <span className={`rounded-full border px-2 py-0.5 text-xs ${meta.className}`}>
                      {t(`settings.channels.${meta.key}`)}
                    </span>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => void toggleEnabled(item)}
                        disabled={isBusy || item.available === false}
                        className={item.enabled ? ghostButton : primaryButton}
                      >
                        {isBusy && busy?.endsWith(":toggle") ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : null}
                        {item.enabled ? t("settings.channels.disable") : t("settings.channels.enable")}
                      </button>
                      {item.enabled && item.running && (
                        <button
                          type="button"
                          onClick={() => void channelAction(name, "stop")}
                          disabled={isBusy}
                          className={ghostButton}
                        >
                          <Square className="h-3.5 w-3.5" />
                          {t("settings.channels.stopOne")}
                        </button>
                      )}
                      {item.enabled && !item.running && item.available !== false && (
                        <button
                          type="button"
                          onClick={() => void channelAction(name, "start")}
                          disabled={isBusy}
                          className={ghostButton}
                        >
                          <Play className="h-3.5 w-3.5" />
                          {t("settings.channels.startOne")}
                        </button>
                      )}
                      {canBind && (
                        <button
                          type="button"
                          onClick={() => void beginBinding(name)}
                          disabled={isBusy || item.available === false}
                          className={primaryButton}
                          title={item.install_hint || undefined}
                        >
                          <QrCode className="h-3.5 w-3.5" />
                          {state === "needs_auth"
                            ? t("settings.channels.rebind")
                            : t("settings.channels.bind")}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setLogsFor(name)}
                        className={ghostButton}
                        title={t("settings.channels.logsHint")}
                      >
                        <ScrollText className="h-3.5 w-3.5" />
                        {t("settings.channels.logs")}
                      </button>
                      {fields && (
                        <button
                          type="button"
                          onClick={() => setExpanded(expanded === name ? null : name)}
                          className={ghostButton}
                        >
                          <ChevronDown
                            className={`h-3.5 w-3.5 transition ${expanded === name ? "rotate-180" : ""}`}
                          />
                          {t("settings.channels.configure")}
                        </button>
                      )}
                      {canBind && (
                        <button
                          type="button"
                          onClick={() => void unbind(name)}
                          disabled={isBusy}
                          className={ghostButton}
                          title={t("settings.channels.unbind")}
                        >
                          <Unlink className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  </div>

                  {(item.last_error || item.install_hint || item.error) && state !== "running" && (
                    <p className="mt-1.5 text-xs text-muted-foreground">
                      {item.install_hint || item.last_error || item.error}
                    </p>
                  )}

                  {item.retry_in_s !== undefined && item.retry_in_s > 0 && item.desired && (
                    <p className="mt-1 text-xs text-amber-600">
                      {t("settings.channels.retryIn", { seconds: Math.ceil(item.retry_in_s) })}
                      {item.restarts ? ` · ${t("settings.channels.restartCount", { count: item.restarts })}` : ""}
                    </p>
                  )}

                  {expanded === name && fields && (
                    <div className="mt-3 grid gap-2 rounded-md border bg-muted/20 p-3 sm:grid-cols-2">
                      {fields.map((field) => (
                        <label key={field.key} className="grid gap-1 text-xs">
                          <span className="font-medium">{field.label}</span>
                          <input
                            type={field.secret ? "password" : "text"}
                            className={fieldClass}
                            value={credentials[field.key] ?? ""}
                            placeholder={t("settings.channels.keepCurrent")}
                            onChange={(event) =>
                              setCredentials((prev) => ({ ...prev, [field.key]: event.target.value }))
                            }
                          />
                        </label>
                      ))}
                      <div className="sm:col-span-2">
                        <button
                          type="button"
                          onClick={() => void saveCredentials(name)}
                          disabled={busy === `${name}:save`}
                          className={primaryButton}
                        >
                          {busy === `${name}:save` ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <ShieldCheck className="h-3.5 w-3.5" />
                          )}
                          {t("settings.channels.saveAndEnable")}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {pending.length > 0 && (
            <div className="mt-4 rounded-md border border-amber-200 bg-amber-50/50 p-3">
              <div className="mb-2 text-xs font-medium text-amber-800">
                {t("settings.channels.pendingPairings")} ({pending.length})
              </div>
              <div className="space-y-2">
                {pending.map((request) => (
                  <div key={request.code} className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded bg-white px-1.5 py-0.5 font-mono">{request.code}</span>
                    <span className="text-muted-foreground">
                      {request.channel} · {request.sender_id}
                    </span>
                    <button
                      type="button"
                      onClick={() => void decidePairing(request.code, true)}
                      disabled={busy === `pair:${request.code}`}
                      className={primaryButton}
                    >
                      <Check className="h-3.5 w-3.5" />
                      {t("settings.channels.approve")}
                    </button>
                    <button
                      type="button"
                      onClick={() => void decidePairing(request.code, false)}
                      disabled={busy === `pair:${request.code}`}
                      className={dangerButton}
                    >
                      <X className="h-3.5 w-3.5" />
                      {t("settings.channels.deny")}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {logsFor && <ChannelLogsDrawer name={logsFor} onClose={() => setLogsFor(null)} />}

      {wizard && (
        <BindingWizard
          name={wizard.name}
          initial={wizard.session}
          onClose={() => setWizard(null)}
          onBound={async () => {
            await refreshAll();
          }}
        />
      )}
    </section>
  );
}

interface BindingWizardProps {
  name: string;
  initial: ChannelAuthSession;
  onClose: () => void;
  onBound: () => Promise<void> | void;
}

function BindingWizard({ name, initial, onClose, onBound }: BindingWizardProps) {
  const { t } = useTranslation();
  const [session, setSession] = useState<ChannelAuthSession>(initial);
  const [restarting, setRestarting] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const terminal = ["confirmed", "failed", "expired"].includes(session.state);
    if (terminal) return;
    const tick = async () => {
      try {
        const next = await api.getChannelAuth(name, session.session_id);
        setSession(next);
      } catch {
        // transient poll failure: keep the current QR on screen
      }
    };
    timer.current = window.setInterval(() => void tick(), 1200);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
      timer.current = null;
    };
  }, [name, session.session_id, session.state]);

  useEffect(() => {
    if (session.state !== "confirmed") return;
    void onBound();
    const timeout = window.setTimeout(onClose, 1200);
    return () => window.clearTimeout(timeout);
  }, [session.state, onBound, onClose]);

  const refreshQr = async () => {
    setRestarting(true);
    try {
      await api.cancelChannelAuth(name, session.session_id).catch(() => undefined);
      const next = await api.startChannelAuth(name);
      setSession(next);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRestarting(false);
    }
  };

  const statusText: Record<string, string> = {
    pending: t("settings.channels.scanPending"),
    waiting_scan: t("settings.channels.scanWaiting"),
    scanned: t("settings.channels.scanScanned"),
    confirmed: t("settings.channels.scanConfirmed"),
    expired: t("settings.channels.scanExpired"),
    failed: session.error || t("settings.channels.scanFailed"),
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-lg border bg-card p-5 shadow-lg">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-sm font-semibold">{t("settings.channels.bindTitle")}</h3>
            <p className="text-xs text-muted-foreground">{name}</p>
          </div>
          <button type="button" onClick={onClose} className={ghostButton} aria-label={t("settings.channels.close")}>
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="mt-4 flex justify-center">
          {session.qr_svg ? (
            <div
              className="rounded-md border bg-white p-3 [&_svg]:h-52 [&_svg]:w-52"
              dangerouslySetInnerHTML={{ __html: session.qr_svg }}
            />
          ) : session.qr_url ? (
            <a href={session.qr_url} target="_blank" rel="noreferrer" className="break-all text-xs text-primary underline">
              {session.qr_url}
            </a>
          ) : (
            <div className="flex h-52 w-52 items-center justify-center rounded-md border bg-muted/20 text-xs text-muted-foreground">
              {session.state === "failed" ? t("settings.channels.scanFailed") : <Loader2 className="h-5 w-5 animate-spin" />}
            </div>
          )}
        </div>

        <p className="mt-3 text-center text-sm">{statusText[session.state] ?? session.detail}</p>
        {session.state === "waiting_scan" && (
          <p className="mt-1 text-center text-xs text-muted-foreground">
            {t("settings.channels.scanHint")}
          </p>
        )}

        <div className="mt-4 flex items-center justify-end gap-2">
          {(session.state === "expired" || session.state === "failed") && (
            <button type="button" onClick={() => void refreshQr()} disabled={restarting} className={primaryButton}>
              {restarting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {t("settings.channels.refreshQr")}
            </button>
          )}
          <button type="button" onClick={onClose} className={ghostButton}>
            {t("settings.channels.close")}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ChannelLogsDrawerProps {
  name: string;
  onClose: () => void;
}

const LEVEL_CLASS: Record<string, string> = {
  error: "text-red-600",
  critical: "text-red-600",
  warning: "text-amber-600",
  info: "text-foreground",
  debug: "text-muted-foreground",
};

/**
 * Recent log lines for one adapter.
 *
 * Adapters log to the service process, which nobody can see when the server
 * runs headless; this drawer is the operator's only view of *why* a channel is
 * not connecting.
 */
function ChannelLogsDrawer({ name, onClose }: ChannelLogsDrawerProps) {
  const { t } = useTranslation();
  const [lines, setLines] = useState<ChannelLogLine[]>([]);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.getChannelLogs(name, 200);
      setLines(result.lines ?? []);
    } catch {
      // transient failure: keep whatever is already on screen
    } finally {
      setLoading(false);
    }
  }, [name]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  const clear = async () => {
    setClearing(true);
    try {
      await api.clearChannelLogs(name);
      setLines([]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-lg border bg-card p-5 shadow-lg">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">{t("settings.channels.logsTitle")}</h3>
            <p className="text-xs text-muted-foreground">{name}</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void load()}
              className={ghostButton}
              aria-label={t("settings.channels.refresh")}
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => void clear()}
              disabled={clearing}
              className={ghostButton}
              aria-label={t("settings.channels.logsClear")}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={onClose}
              className={ghostButton}
              aria-label={t("settings.channels.close")}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <div className="mt-3 min-h-[220px] flex-1 overflow-auto rounded-md border bg-muted/20 p-3 font-mono text-[11px] leading-relaxed">
          {loading ? (
            <div className="flex h-full items-center justify-center text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : lines.length === 0 ? (
            <div className="flex h-full items-center justify-center text-muted-foreground">
              {t("settings.channels.logsEmpty")}
            </div>
          ) : (
            lines.map((line, index) => (
              <div key={`${line.ts}-${index}`} className={LEVEL_CLASS[line.level] ?? "text-foreground"}>
                <span className="text-muted-foreground">
                  {new Date(line.ts * 1000).toLocaleTimeString()}{" "}
                  {line.level.toUpperCase().padEnd(7)}
                </span>
                <span className="whitespace-pre-wrap break-words">{line.message}</span>
              </div>
            ))
          )}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">{t("settings.channels.logsHint")}</p>
      </div>
    </div>
  );
}
