// These assignments exercise the generated types rather than only parsing them.
import type {
  CreateRequest,
  IdentityListRequest,
  IdentityWhoamiRequest,
  Task,
  TopicName,
  VersionRequest,
} from "../service/src/generated/protocol.js";
import { validateCreateRequest } from "../service/src/generated/validators.js";

export const topic: TopicName = "work/linux";
export const request: CreateRequest = {
  domain: "acme/pipeline",
  topic,
  data: { nested: [{ strings: ["hello"], numbers: [1, 2.5], null: null }] },
};

export function validate(body: unknown): string | null {
  return validateCreateRequest(body) ? body.domain.toUpperCase() : null;
}

export function readTask(task: Task): string {
  return task.topic;
}

// @ts-expect-error Topic names are strings, not objects.
export const invalidTopic: TopicName = {};
export const invalidData: CreateRequest = {
  domain: "acme",
  topic: "work",
  // @ts-expect-error data is an object, not an array.
  data: [],
};

// Closed empty objects must reject non-objects and populated objects.
export const identityList: IdentityListRequest = {};
export const identityWhoami: IdentityWhoamiRequest = {};
export const version: VersionRequest = {};
// @ts-expect-error A primitive is not an empty object.
export const identityListPrimitive: IdentityListRequest = 42;
// @ts-expect-error An array is not an empty object.
export const identityListArray: IdentityListRequest = [];
// @ts-expect-error Closed requests reject unexpected properties.
export const identityListExtra: IdentityListRequest = { extra: true };
// @ts-expect-error A primitive is not an empty object.
export const identityWhoamiPrimitive: IdentityWhoamiRequest = "token";
// @ts-expect-error An array is not an empty object.
export const identityWhoamiArray: IdentityWhoamiRequest = [];
// @ts-expect-error Closed requests reject unexpected properties.
export const identityWhoamiExtra: IdentityWhoamiRequest = { extra: true };
// @ts-expect-error A primitive is not an empty object.
export const versionPrimitive: VersionRequest = true;
// @ts-expect-error An array is not an empty object.
export const versionArray: VersionRequest = [];
// @ts-expect-error Closed requests reject unexpected properties.
export const versionExtra: VersionRequest = { extra: true };
