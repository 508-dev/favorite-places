import { createHash } from "node:crypto";
import { resolve } from "node:path";

export const DEFAULT_WORKTREE_DEV_BASE_PORT = 4321;
export const DEFAULT_WORKTREE_DEV_PORT_SPAN = 1000;
export const CONDUCTOR_WORKTREE_DEV_PORT_SPAN = 10;
export const CONDUCTOR_PORT_ENV = "CONDUCTOR_PORT";
export const WORKTREE_DEV_BASE_PORT_ENV = "WORKTREE_DEV_BASE_PORT";
export const WORKTREE_DEV_PORT_ENV = "WORKTREE_DEV_PORT";
export const WORKTREE_DEV_PORT_OFFSET_ENV = "WORKTREE_DEV_PORT_OFFSET";
export const WORKTREE_DEV_PORT_SPAN_ENV = "WORKTREE_DEV_PORT_SPAN";
export const WORKTREE_DEV_ROOT_ENV = "WORKTREE_DEV_ROOT";

const MAX_PORT = 65535;
const BROWSER_UNSAFE_PORTS = new Set([
  1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102,
  103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465,
  512, 513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993,
  995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
  6697, 10080,
]);

function parsePortLike(value, name) {
  if (value === undefined || value === "") {
    return undefined;
  }

  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > MAX_PORT) {
    throw new Error(`${name} must be an integer from 1 to ${MAX_PORT}.`);
  }

  return parsed;
}

function parsePositiveInteger(value, name) {
  if (value === undefined || value === "") {
    return undefined;
  }

  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${name} must be a positive integer.`);
  }

  return parsed;
}

export function worktreePathKey(worktreeRoot) {
  return resolve(worktreeRoot)
    .split(/[\\/]+/)
    .filter(Boolean)
    .join("/");
}

export function worktreePortOffset(worktreeRoot, span) {
  const digest = createHash("sha256").update(worktreePathKey(worktreeRoot)).digest();
  return digest.readUInt32BE(0) % span;
}

export function isBrowserUnsafePort(port) {
  return BROWSER_UNSAFE_PORTS.has(port);
}

function safePortsInRange(basePort, span) {
  const safePorts = [];
  for (let port = basePort; port < basePort + span; port += 1) {
    if (!isBrowserUnsafePort(port)) {
      safePorts.push(port);
    }
  }

  return safePorts;
}

export function resolveWorktreeDevPort({ env = process.env, worktreeRoot }) {
  if (!worktreeRoot) {
    throw new Error("worktreeRoot is required to resolve the dev server port.");
  }

  const explicitPort =
    parsePortLike(env[WORKTREE_DEV_PORT_ENV], WORKTREE_DEV_PORT_ENV) ??
    parsePortLike(env.PORT, "PORT");

  const pathKey = worktreePathKey(worktreeRoot);
  if (explicitPort !== undefined) {
    const span = DEFAULT_WORKTREE_DEV_PORT_SPAN;
    return {
      basePort: DEFAULT_WORKTREE_DEV_BASE_PORT,
      offset: worktreePortOffset(worktreeRoot, span),
      pathKey,
      port: explicitPort,
      portIsBrowserUnsafe: isBrowserUnsafePort(explicitPort),
      span,
      usingExplicitPort: true,
    };
  }

  const conductorBasePort = parsePortLike(env[CONDUCTOR_PORT_ENV], CONDUCTOR_PORT_ENV);
  const basePort =
    parsePortLike(env[WORKTREE_DEV_BASE_PORT_ENV], WORKTREE_DEV_BASE_PORT_ENV) ??
    conductorBasePort ??
    DEFAULT_WORKTREE_DEV_BASE_PORT;
  const span =
    parsePositiveInteger(env[WORKTREE_DEV_PORT_SPAN_ENV], WORKTREE_DEV_PORT_SPAN_ENV) ??
    (conductorBasePort === undefined
      ? DEFAULT_WORKTREE_DEV_PORT_SPAN
      : CONDUCTOR_WORKTREE_DEV_PORT_SPAN);

  if (basePort + span - 1 > MAX_PORT) {
    throw new Error(
      `${WORKTREE_DEV_BASE_PORT_ENV} + ${WORKTREE_DEV_PORT_SPAN_ENV} - 1 must not exceed ${MAX_PORT}.`,
    );
  }

  const safePorts = safePortsInRange(basePort, span);
  if (safePorts.length === 0) {
    throw new Error(
      `range ${WORKTREE_DEV_BASE_PORT_ENV}..(${WORKTREE_DEV_BASE_PORT_ENV} + ${WORKTREE_DEV_PORT_SPAN_ENV} - 1) (${basePort}..${basePort + span - 1}) must include at least one browser-safe port.`,
    );
  }

  const derivedPort = safePorts[worktreePortOffset(worktreeRoot, safePorts.length)];
  const offset = derivedPort - basePort;

  return {
    basePort,
    offset,
    pathKey,
    port: derivedPort,
    portIsBrowserUnsafe: false,
    span,
    usingExplicitPort: false,
  };
}
