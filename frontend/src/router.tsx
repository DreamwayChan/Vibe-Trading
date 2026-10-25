import { Suspense, type ComponentType } from "react";
import { createBrowserRouter, isRouteErrorResponse, useRouteError } from "react-router-dom";
import { AlertTriangle, RefreshCw } from "lucide-react";
import i18n from "@/i18n";
import { Layout } from "@/components/layout/Layout";
import { lazyWithReload } from "@/lib/lazyWithReload";

const Home = lazyWithReload(() => import("@/pages/Home").then((m) => ({ default: m.Home })));
const Agent = lazyWithReload(() => import("@/pages/Agent").then((m) => ({ default: m.Agent })));
const RunDetail = lazyWithReload(() =>
  import("@/pages/RunDetail").then((m) => ({ default: m.RunDetail })),
);
const Compare = lazyWithReload(() =>
  import("@/pages/Compare").then((m) => ({ default: m.Compare })),
);
const Settings = lazyWithReload(() =>
  import("@/pages/Settings").then((m) => ({ default: m.Settings })),
);
const Runtime = lazyWithReload(() =>
  import("@/pages/Runtime").then((m) => ({ default: m.Runtime })),
);
const Reports = lazyWithReload(() =>
  import("@/pages/Reports").then((m) => ({ default: m.Reports })),
);
const Correlation = lazyWithReload(() =>
  import("@/pages/Correlation").then((m) => ({ default: m.Correlation })),
);
const Monitor = lazyWithReload(() =>
  import("@/pages/Monitor").then((m) => ({ default: m.Monitor })),
);
const MarketPick = lazyWithReload(() =>
  import("@/pages/MarketPick").then((m) => ({ default: m.MarketPick })),
);
const SectorPick = lazyWithReload(() =>
  import("@/pages/SectorPick").then((m) => ({ default: m.SectorPick })),
);
const AlphaZoo = lazyWithReload(() =>
  import("@/pages/AlphaZoo").then((m) => ({ default: m.AlphaZoo })),
);

function PageLoader() {
  return (
    <div className="flex h-[60vh] items-center justify-center text-muted-foreground">
      Loading…
    </div>
  );
}

function wrap(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoader />}>
      <Component />
    </Suspense>
  );
}

/** Route-level fallback when a lazy page chunk fails after the one-shot reload. */
function RouteErrorPage() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? error.statusText || String(error.status)
    : error instanceof Error
      ? error.message
      : i18n.t("errorBoundary.somethingWrong");

  return (
    <div className="mx-auto flex max-w-lg flex-col items-center gap-4 px-6 py-16 text-center">
      <AlertTriangle className="h-8 w-8 text-destructive" />
      <div className="space-y-2">
        <h1 className="text-lg font-semibold">{i18n.t("errorBoundary.pageLoadFailed")}</h1>
        <p className="text-sm text-muted-foreground break-words">{message}</p>
      </div>
      <button
        type="button"
        className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted"
        onClick={() => window.location.reload()}
      >
        <RefreshCw className="h-4 w-4" />
        {i18n.t("errorBoundary.reloadPage")}
      </button>
    </div>
  );
}

export const router = createBrowserRouter([
  {
    element: <Layout />,
    errorElement: <RouteErrorPage />,
    children: [
      { path: "/", element: wrap(Home) },
      { path: "/agent", element: wrap(Agent) },
      { path: "/runtime", element: wrap(Runtime) },
      { path: "/reports", element: wrap(Reports) },
      { path: "/settings", element: wrap(Settings) },
      { path: "/runs/:runId", element: wrap(RunDetail), errorElement: <RouteErrorPage /> },
      { path: "/compare", element: wrap(Compare) },
      { path: "/correlation", element: wrap(Correlation) },
      { path: "/monitor", element: wrap(Monitor) },
      { path: "/market-pick", element: wrap(MarketPick) },
      { path: "/sector-pick", element: wrap(SectorPick) },
      { path: "/alpha-zoo", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/bench", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/compare", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/:alphaId", element: wrap(AlphaZoo) },
    ],
  },
]);
