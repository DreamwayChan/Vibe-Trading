import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tables } from "../Tables";

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
    ApiError,
    api: {
      getTableEngine: vi.fn(),
      uploadFile: vi.fn(),
      extractTables: vi.fn(),
    },
  };
});

vi.mock("@/lib/api", () => ({ api: mocks.api, ApiError: mocks.ApiError }));

const ENGINE_OK = {
  status: "ok" as const,
  requested: "auto",
  engine: "heuristic",
  version: "1",
  quality: "geometric",
  available: ["heuristic"],
  max_pages: 15,
  cache: true,
};

const RESULT = {
  status: "ok" as const,
  file: "report.pdf",
  format: "pdf",
  engine: "heuristic",
  engine_version: "1",
  quality: "geometric" as const,
  pages_requested: [3],
  pages_read: [3],
  table_count: 1,
  char_count: 120,
  truncated: false,
  elapsed_ms: 42,
  cache: "miss" as const,
  tables: [
    {
      id: "t1",
      page: 3,
      pages: [3],
      caption: "Revenue breakdown",
      n_rows: 2,
      n_cols: 2,
      header_rows: 1,
      columns: ["Segment", "2025E"],
      column_types: ["text", "number"],
      rows: [
        ["Cloud", "15,900"],
        ["Total", "30,650"],
      ],
      unit_label: "RMB mn" as string | null,
      unit_scale: null,
      statement_hint: null,
      markdown: "| Segment | 2025E |",
      csv: "Segment,2025E\nCloud,15900",
      truncated: false,
      confidence: null,
    },
  ],
};

function pdfFile(name = "report.pdf") {
  return new File(["%PDF-1.4"], name, { type: "application/pdf" });
}

describe("Tables page", () => {
  beforeEach(() => {
    mocks.api.getTableEngine.mockReset().mockResolvedValue(ENGINE_OK);
    mocks.api.uploadFile.mockReset();
    mocks.api.extractTables.mockReset();
  });

  it("renders the drop zone and the engine banner", async () => {
    render(<Tables />);

    expect(screen.getByText("Research Report Table Extraction")).toBeInTheDocument();
    expect(await screen.findByText(/Engine heuristic 1/)).toBeInTheDocument();
    expect(screen.getByText("Drop a PDF here")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Extract tables/ })).toBeDisabled();
  });

  it("uploads the PDF then renders the extracted grid", async () => {
    mocks.api.uploadFile.mockResolvedValue({
      status: "ok",
      file_path: "uploads/abc.pdf",
      filename: "report.pdf",
    });
    mocks.api.extractTables.mockResolvedValue(RESULT);

    render(<Tables />);
    const user = userEvent.setup();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, pdfFile());

    const runButton = await screen.findByRole("button", { name: /Extract tables/ });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);

    expect(await screen.findByText("Revenue breakdown")).toBeInTheDocument();
    expect(mocks.api.extractTables).toHaveBeenCalledWith({
      file_path: "uploads/abc.pdf",
      pages: "",
      max_tables: 20,
      include_csv: true,
    });
    expect(screen.getByText("15,900")).toBeInTheDocument();
    expect(screen.getByText("30,650")).toBeInTheDocument();
    expect(screen.getByText(/p\. 3/)).toBeInTheDocument();
    expect(screen.getByText(/RMB mn/)).toBeInTheDocument();
    expect(screen.getByText("2 rows × 2 cols")).toBeInTheDocument();
  });

  it("rejects a non-PDF selection without calling the API", async () => {
    render(<Tables />);
    // applyAccept=false so the browser-level `accept` filter does not swallow the
    // file before the page's own validation runs.
    const user = userEvent.setup({ applyAccept: false });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["x"], "notes.txt", { type: "text/plain" }));

    expect(await screen.findByText("Only PDF files are supported.")).toBeInTheDocument();
    expect(mocks.api.uploadFile).not.toHaveBeenCalled();
  });

  it("surfaces an extraction error from the API", async () => {
    mocks.api.uploadFile.mockResolvedValue({
      status: "ok",
      file_path: "uploads/abc.pdf",
      filename: "report.pdf",
    });
    mocks.api.extractTables.mockRejectedValue(new mocks.ApiError("File not found: uploads/abc.pdf", 400));

    render(<Tables />);
    const user = userEvent.setup();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, pdfFile());
    const runButton = await screen.findByRole("button", { name: /Extract tables/ });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);

    expect(await screen.findByText("File not found: uploads/abc.pdf")).toBeInTheDocument();
  });

  it("shows the empty state when no table is found", async () => {
    mocks.api.uploadFile.mockResolvedValue({
      status: "ok",
      file_path: "uploads/abc.pdf",
      filename: "report.pdf",
    });
    mocks.api.extractTables.mockResolvedValue({ ...RESULT, table_count: 0, tables: [] });

    render(<Tables />);
    const user = userEvent.setup();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, pdfFile());
    const runButton = await screen.findByRole("button", { name: /Extract tables/ });
    await waitFor(() => expect(runButton).toBeEnabled());
    await user.click(runButton);

    expect(await screen.findByText("No table detected")).toBeInTheDocument();
  });
});
