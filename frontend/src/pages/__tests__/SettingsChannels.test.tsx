import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Settings } from "../Settings";

const apiMock = vi.hoisted(() => ({
  getLLMSettings: vi.fn(),
  getDataSourceSettings: vi.fn(),
  getChannelStatus: vi.fn(),
  startChannels: vi.fn(),
  stopChannels: vi.fn(),
  getChannelSettings: vi.fn(),
  updateChannelSettings: vi.fn(),
  getPendingPairings: vi.fn(),
  startChannel: vi.fn(),
  stopChannel: vi.fn(),
  restartChannel: vi.fn(),
  patchChannel: vi.fn(),
  startChannelAuth: vi.fn(),
  getChannelAuth: vi.fn(),
  cancelChannelAuth: vi.fn(),
  unbindChannel: vi.fn(),
  approvePairing: vi.fn(),
  denyPairing: vi.fn(),
  getChannelLogs: vi.fn(),
  clearChannelLogs: vi.fn(),
  updateLLMSettings: vi.fn(),
  updateDataSourceSettings: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: apiMock,
    isAuthRequiredError: vi.fn(() => false),
  };
});

vi.mock("@/lib/apiAuth", () => ({
  getApiAuthKey: vi.fn(() => ""),
  setApiAuthKey: vi.fn(),
}));

function llmSettings() {
  return {
    provider: "openrouter",
    model_name: "deepseek/deepseek-v3.2",
    base_url: "https://openrouter.ai/api/v1",
    api_key_env: "OPENROUTER_API_KEY",
    api_key_configured: false,
    api_key_required: true,
    temperature: 0.1,
    timeout_seconds: 120,
    max_retries: 2,
    reasoning_effort: "",
    sse_timeout_seconds: 300,
    env_path: "agent/.env",
    providers: [
      {
        name: "openrouter",
        label: "OpenRouter",
        api_key_env: "OPENROUTER_API_KEY",
        base_url_env: "OPENROUTER_BASE_URL",
        default_model: "deepseek/deepseek-v3.2",
        default_base_url: "https://openrouter.ai/api/v1",
        api_key_required: true,
        auth_type: "api_key",
      },
    ],
  };
}

function dataSourceSettings() {
  return {
    tushare_token_configured: false,
    baostock_supported: true,
    baostock_installed: true,
    baostock_message: "BaoStock available",
    env_path: "agent/.env",
  };
}

function channelStatus(overrides = {}) {
  return {
    running: false,
    inbound_queue: 0,
    outbound_queue: 0,
    session_count: 0,
    channels: {
      websocket: {
        name: "websocket",
        display_name: "WebSocket",
        configured: true,
        enabled: true,
        available: true,
        loaded: true,
        running: false,
        error: "",
        install_hint: "",
      },
      telegram: {
        name: "telegram",
        display_name: "Telegram",
        configured: true,
        enabled: false,
        available: false,
        loaded: false,
        running: false,
        error: "ModuleNotFoundError",
        install_hint: "pip install 'vibe-trading-ai[telegram]'",
      },
      weixin: {
        name: "weixin",
        display_name: "WeChat",
        configured: true,
        enabled: true,
        available: true,
        loaded: true,
        running: false,
        state: "needs_auth",
        auth_kind: "qr",
        error: "",
        install_hint: "",
      },
    },
    ...overrides,
  };
}

describe("Settings IM channels panel", () => {
  beforeEach(() => {
    apiMock.getLLMSettings.mockResolvedValue(llmSettings());
    apiMock.getDataSourceSettings.mockResolvedValue(dataSourceSettings());
    apiMock.getChannelStatus.mockResolvedValue(channelStatus());
    apiMock.getChannelSettings.mockResolvedValue({ auto_start: true, env_override: null, env_locked: false });
    apiMock.getPendingPairings.mockResolvedValue({ pending: [] });
    apiMock.startChannels.mockResolvedValue(channelStatus({ running: true }));
    apiMock.stopChannels.mockResolvedValue(channelStatus());
  });

  it("renders channel runtime status and refreshes it", async () => {
    render(<Settings />);

    expect(await screen.findByText("IM Channels")).toBeInTheDocument();
    expect(screen.getByText("websocket")).toBeInTheDocument();
    expect(screen.getByText("telegram")).toBeInTheDocument();
    expect(screen.getByText("pip install 'vibe-trading-ai[telegram]'")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(apiMock.getChannelStatus).toHaveBeenCalledTimes(2));
  });

  it("starts channels from the settings control surface", async () => {
    render(<Settings />);
    await screen.findByText("IM Channels");

    fireEvent.click(screen.getByRole("button", { name: "Start channels" }));

    await waitFor(() => expect(apiMock.startChannels).toHaveBeenCalledTimes(1));
  });

  it("still renders LLM and data source settings when channel status fails", async () => {
    apiMock.getChannelStatus.mockRejectedValue(
      new Error('Expected JSON from /channels/status, got text/html: <!doctype html>'),
    );

    render(<Settings />);

    expect(await screen.findByText("LLM Settings")).toBeInTheDocument();
    expect(screen.getByText("Data Source Settings")).toBeInTheDocument();
    expect(screen.getByText("IM Channels")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start channels" })).toBeDisabled();
  });

  it("persists the autostart toggle", async () => {
    apiMock.updateChannelSettings.mockResolvedValue({
      auto_start: false,
      env_override: null,
      env_locked: false,
    });

    render(<Settings />);
    await screen.findByText("IM Channels");

    const autoStartButton = screen.getByRole("button", { name: /Auto-connect on start/ });
    await waitFor(() => expect(autoStartButton).toBeEnabled());
    fireEvent.click(autoStartButton);

    await waitFor(() =>
      expect(apiMock.updateChannelSettings).toHaveBeenCalledWith({ auto_start: false }),
    );
  });

  it("opens the QR binding wizard for scan-based channels", async () => {
    const session = {
      session_id: "sess-1",
      channel: "weixin",
      kind: "qr" as const,
      state: "waiting_scan" as const,
      qr_svg: "<svg></svg>",
      expires_in: 180,
    };
    apiMock.startChannelAuth.mockResolvedValue(session);
    apiMock.getChannelAuth.mockResolvedValue(session);

    render(<Settings />);
    await screen.findByText("IM Channels");

    fireEvent.click(screen.getByRole("button", { name: "Re-bind" }));

    await waitFor(() => expect(apiMock.startChannelAuth).toHaveBeenCalledWith("weixin"));
    expect(await screen.findByText("Bind channel")).toBeInTheDocument();
    expect(screen.getByText("Scan the QR code with the app")).toBeInTheDocument();
  });

  it("shows the buffered log lines for a channel", async () => {
    apiMock.getChannelLogs.mockResolvedValue({
      channel: "weixin",
      lines: [
        {
          ts: 1_700_000_000,
          level: "error",
          source: "src.channels.weixin",
          message: "getUpdates failed: ret=500",
        },
      ],
    });

    render(<Settings />);
    await screen.findByText("IM Channels");

    fireEvent.click(screen.getAllByRole("button", { name: "Logs" })[0]);

    expect(await screen.findByText(/getUpdates failed/)).toBeInTheDocument();
    await waitFor(() => expect(apiMock.getChannelLogs).toHaveBeenCalled());
  });
});
