import type {
  ErrorResponse,
  IdentityName,
  Scopes,
  WorkerLabel,
} from "./generated/protocol";

/** The Worker authenticates and authorizes the domain before making DO calls. */
export interface Caller {
  identity: IdentityName;
  worker: WorkerLabel;
  operator: boolean;
  scopes: Scopes;
}

export type Result<T extends { ok: true }> = T | ErrorResponse;
