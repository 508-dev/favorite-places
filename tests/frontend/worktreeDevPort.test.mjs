import { describe, expect, it } from "vitest";

import {
  CONDUCTOR_WORKTREE_DEV_PORT_SPAN,
  DEFAULT_WORKTREE_DEV_BASE_PORT,
  DEFAULT_WORKTREE_DEV_PORT_SPAN,
  isBrowserUnsafePort,
  resolveWorktreeDevPort,
  worktreePathKey,
} from "../../scripts/worktree_dev_port.mjs";

describe("worktree dev port", () => {
  it("derives a stable port from the worktree root path", () => {
    const first = resolveWorktreeDevPort({ env: {}, worktreeRoot: "/repo/alpha" });
    const second = resolveWorktreeDevPort({ env: {}, worktreeRoot: "/repo/alpha" });

    expect(second).toEqual(first);
    expect(first.port).toBeGreaterThanOrEqual(DEFAULT_WORKTREE_DEV_BASE_PORT);
    expect(first.port).toBeLessThan(
      DEFAULT_WORKTREE_DEV_BASE_PORT + DEFAULT_WORKTREE_DEV_PORT_SPAN,
    );
    expect(isBrowserUnsafePort(first.port)).toBe(false);
  });

  it("uses configured base port and span for the deterministic offset", () => {
    const config = resolveWorktreeDevPort({
      env: {
        WORKTREE_DEV_BASE_PORT: "5100",
        WORKTREE_DEV_PORT_SPAN: "50",
      },
      worktreeRoot: "/repo/beta",
    });

    expect(config.basePort).toBe(5100);
    expect(config.span).toBe(50);
    expect(config.port).toBe(5100 + config.offset);
    expect(config.offset).toBeGreaterThanOrEqual(0);
    expect(config.offset).toBeLessThan(50);
    expect(isBrowserUnsafePort(config.port)).toBe(false);
  });

  it("uses the Conductor assigned ten-port range when CONDUCTOR_PORT is set", () => {
    const config = resolveWorktreeDevPort({
      env: { CONDUCTOR_PORT: "7100" },
      worktreeRoot: "/repo/conductor",
    });

    expect(config.basePort).toBe(7100);
    expect(config.span).toBe(CONDUCTOR_WORKTREE_DEV_PORT_SPAN);
    expect(config.port).toBeGreaterThanOrEqual(7100);
    expect(config.port).toBeLessThan(7110);
    expect(config.offset).toBeGreaterThanOrEqual(0);
    expect(config.offset).toBeLessThan(10);
    expect(isBrowserUnsafePort(config.port)).toBe(false);
  });

  it("lets manual derivation settings override the Conductor range", () => {
    const config = resolveWorktreeDevPort({
      env: {
        CONDUCTOR_PORT: "7100",
        WORKTREE_DEV_BASE_PORT: "5200",
        WORKTREE_DEV_PORT_SPAN: "20",
      },
      worktreeRoot: "/repo/conductor-manual",
    });

    expect(config.basePort).toBe(5200);
    expect(config.span).toBe(20);
    expect(config.port).toBeGreaterThanOrEqual(5200);
    expect(config.port).toBeLessThan(5220);
  });

  it("keeps the default span when only the base port manually overrides Conductor", () => {
    const config = resolveWorktreeDevPort({
      env: {
        CONDUCTOR_PORT: "7100",
        WORKTREE_DEV_BASE_PORT: "5200",
      },
      worktreeRoot: "/repo/conductor-manual-base",
    });

    expect(config.basePort).toBe(5200);
    expect(config.span).toBe(DEFAULT_WORKTREE_DEV_PORT_SPAN);
    expect(config.port).toBeGreaterThanOrEqual(5200);
    expect(config.port).toBeLessThan(5200 + DEFAULT_WORKTREE_DEV_PORT_SPAN);
  });

  it("lets an explicit worktree port override the derived port", () => {
    const config = resolveWorktreeDevPort({
      env: { WORKTREE_DEV_PORT: "6200" },
      worktreeRoot: "/repo/gamma",
    });

    expect(config.port).toBe(6200);
    expect(config.usingExplicitPort).toBe(true);
  });

  it("uses PORT as a fallback explicit override", () => {
    const config = resolveWorktreeDevPort({
      env: { PORT: "6300" },
      worktreeRoot: "/repo/delta",
    });

    expect(config.port).toBe(6300);
    expect(config.usingExplicitPort).toBe(true);
  });

  it("does not validate derivation settings when an explicit port is set", () => {
    const config = resolveWorktreeDevPort({
      env: {
        PORT: "6400",
        WORKTREE_DEV_BASE_PORT: "99999",
        WORKTREE_DEV_PORT_SPAN: "0",
      },
      worktreeRoot: "/repo/epsilon",
    });

    expect(config.port).toBe(6400);
    expect(config.usingExplicitPort).toBe(true);
  });

  it("skips browser-unsafe ports when deriving a worktree port", () => {
    const config = resolveWorktreeDevPort({
      env: {
        WORKTREE_DEV_BASE_PORT: "5059",
        WORKTREE_DEV_PORT_SPAN: "4",
      },
      worktreeRoot: "/repo/zeta",
    });

    expect(config.port).toBeGreaterThanOrEqual(5059);
    expect(config.port).toBeLessThan(5063);
    expect([5059, 5062]).toContain(config.port);
    expect(isBrowserUnsafePort(config.port)).toBe(false);
  });

  it("fails when a configured derivation range only contains browser-unsafe ports", () => {
    expect(() =>
      resolveWorktreeDevPort({
        env: {
          WORKTREE_DEV_BASE_PORT: "5060",
          WORKTREE_DEV_PORT_SPAN: "2",
        },
        worktreeRoot: "/repo/eta",
      }),
    ).toThrow(/browser-safe port/);
  });

  it("normalizes paths into a filesystem-root-relative key", () => {
    expect(worktreePathKey("/Users/example/project")).toBe("Users/example/project");
  });
});
