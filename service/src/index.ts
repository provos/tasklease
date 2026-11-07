export { DomainObject } from "./domain_do";
export { RegistryObject } from "./registry_do";

export default {
  fetch(request: Request): Response {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/v1/version") {
      return Response.json({ service_version: "0.1.0", protocol: "v1" });
    }
    return Response.json(
      { ok: false, error: { code: "invalid", message: "Unknown operation" } },
      { status: 400 },
    );
  },
} satisfies ExportedHandler<Env>;
