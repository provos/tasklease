/**
 * Vite plugin that serves `/api/snapshot` from mock scenarios, so the
 * dashboard runs without a service, credentials or local agent stores.
 *
 * Start it with `npm run monitor:mock` (dev server with hot reload) or
 * `npm run monitor:mock:preview` (the production build). Choose a scenario
 * in the page URL, for example `/?scenario=empty&delay=3000`; the dashboard's
 * snapshot request carries the page URL as its Referer. `TL_MOCK_SCENARIO`
 * sets the default scenario.
 */
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";
import {
  isScenario,
  mockSnapshot,
  SCENARIOS,
  type Scenario,
} from "./snapshot.ts";

const MAX_DELAY_MS = 30_000;

export interface MockResponse {
  status: number;
  contentType: string;
  body: string;
  delayMs: number;
}

function text(status: number, message: string): MockResponse {
  return {
    status,
    contentType: "text/plain; charset=utf-8",
    body: `${message}\n`,
    delayMs: 0,
  };
}

/** Options from the page URL, which the browser sends as the Referer. */
function pageOptions(
  referer: string | undefined,
  fallback: string,
): { scenario: string; delay: string } {
  let params = new URLSearchParams();
  if (referer) {
    try {
      params = new URL(referer).searchParams;
    } catch {
      // A malformed Referer selects the defaults, as no Referer does.
    }
  }
  return {
    scenario: params.get("scenario") ?? fallback,
    delay: params.get("delay") ?? "0",
  };
}

/** The response for a snapshot request, or null for any other path. */
export function respond(
  url: string,
  referer: string | undefined,
  defaultScenario: string,
  now: number,
): MockResponse | null {
  const target = new URL(url, "http://mock.invalid");
  if (target.pathname !== "/api/snapshot") return null;
  const hoursText = target.searchParams.get("hours") ?? "24";
  const hours = /^[0-9]{1,3}$/.test(hoursText) ? Number(hoursText) : 0;
  if (hours < 1 || hours > 720) return text(400, "Invalid request");
  const options = pageOptions(referer, defaultScenario);
  if (!isScenario(options.scenario)) {
    return text(
      400,
      `Unknown mock scenario "${options.scenario}"; choose ${SCENARIOS.join(", ")}`,
    );
  }
  const delayMs = /^[0-9]{1,5}$/.test(options.delay)
    ? Math.min(Number(options.delay), MAX_DELAY_MS)
    : 0;
  const scenario: Scenario = options.scenario;
  if (scenario === "unreachable") {
    return { ...text(502, "The service could not be reached"), delayMs };
  }
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(mockSnapshot(scenario, hours, now)),
    delayMs,
  };
}

export function mockMonitorApi(
  defaultScenario = process.env.TL_MOCK_SCENARIO ?? "busy",
): Plugin {
  const handle = (
    request: IncomingMessage,
    response: ServerResponse,
    next: () => void,
  ): void => {
    const result =
      request.method === "GET"
        ? respond(
            request.url ?? "/",
            request.headers.referer,
            defaultScenario,
            Date.now(),
          )
        : null;
    if (result === null) {
      next();
      return;
    }
    setTimeout(() => {
      response.writeHead(result.status, {
        "Content-Type": result.contentType,
        "Cache-Control": "no-store",
      });
      response.end(result.body);
    }, result.delayMs);
  };
  const hint =
    `  Mock /api/snapshot: default scenario "${defaultScenario}". ` +
    `Open /?scenario=${SCENARIOS.join("|")} (add &delay=MS to slow it).`;
  return {
    name: "tasklease-mock-monitor-api",
    configureServer(server) {
      server.middlewares.use(handle);
      const printUrls = server.printUrls.bind(server);
      server.printUrls = () => {
        printUrls();
        server.config.logger.info(hint);
      };
    },
    configurePreviewServer(server) {
      server.middlewares.use(handle);
      const printUrls = server.printUrls.bind(server);
      server.printUrls = () => {
        printUrls();
        server.config.logger.info(hint);
      };
    },
  };
}
