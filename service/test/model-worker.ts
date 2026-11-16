/** Local model-testing entrypoint. Never imported by the production Worker. */
import worker, { DomainObject as ProductionDomainObject } from "../src/index";

export { RegistryObject } from "../src/index";

// Each isolate installs the clock shim; the control RPC sets the clock in the
// domain's isolate as well. This process runs serial model histories only.
const wallNow = Date.now;
let modelNow: number | undefined;
Date.now = () => modelNow ?? wallNow();
const randomValues = crypto.getRandomValues.bind(crypto);
let idSeed = 0;
crypto.getRandomValues = (array) => {
  if (
    modelNow === undefined ||
    !(array instanceof Uint8Array) ||
    array.length !== 10
  ) {
    randomValues(array);
    return array;
  }
  // Deterministic, non-monotonic ULID suffixes make equal-time ordering and
  // Hypothesis shrinking reproducible. Other cryptographic calls stay real.
  for (let index = 0; index < array.length; index++) {
    idSeed ^= idSeed << 13;
    idSeed ^= idSeed >>> 17;
    idSeed ^= idSeed << 5;
    array[index] = idSeed & 255;
  }
  return array;
};

export class DomainObject extends ProductionDomainObject {
  setModelTime(now: number, reset: boolean): void {
    modelNow = now;
    if (reset) idSeed = 0x6d2b79f5;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (new URL(request.url).pathname === "/__model/clock") {
      const body = (await request.json()) as {
        domain: string;
        now: number;
        reset: boolean;
      };
      if (
        request.method !== "POST" ||
        !Number.isSafeInteger(body.now) ||
        body.now < 0
      ) {
        return new Response("Invalid model clock", { status: 400 });
      }
      const domains = env.DOMAINS as DurableObjectNamespace<DomainObject>;
      await domains.getByName(body.domain).setModelTime(body.now, body.reset);
      return Response.json({ ok: true });
    }
    return worker.fetch(request, env);
  },
} satisfies ExportedHandler<Env>;
