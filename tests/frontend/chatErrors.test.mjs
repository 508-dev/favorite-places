import { describe, expect, it, vi } from "vitest";
import {
  dailyQuotaDetails,
  isDailyAiQuotaError,
  notifyQuotaExhausted,
} from "../../functions/lib/chat-errors.js";
import { DailyQuotaAlerts } from "../../workers/quota-alerts/index.js";

describe("AI quota errors", () => {
  it("recognizes account exhaustion without confusing transient capacity or rate limits", () => {
    expect(isDailyAiQuotaError({ code: "3036" })).toBe(true);
    expect(
      isDailyAiQuotaError(
        new Error("4006: you have used up your daily free allocation of 10,000 neurons"),
      ),
    ).toBe(true);
    expect(isDailyAiQuotaError({ cause: { errors: [{ code: 3036 }] } })).toBe(true);
    expect(isDailyAiQuotaError({ code: 4006, message: "Service temporarily at capacity" })).toBe(
      false,
    );
    expect(isDailyAiQuotaError({ status: 429, message: "Too many requests" })).toBe(false);
  });
  it("reports the next UTC reset across month and year boundaries", () => {
    expect(dailyQuotaDetails(new Date("2026-12-31T23:59:59Z")).resets_at).toBe(
      "2027-01-01T00:00:00.000Z",
    );
    expect(dailyQuotaDetails(new Date("2028-02-28T23:59:59Z")).resets_at).toBe(
      "2028-02-29T00:00:00.000Z",
    );
  });
  it("does not send preview alerts and isolates notification failures", async () => {
    const fetch = vi.fn(async () => new Response("Failed", { status: 502 }));
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      await notifyQuotaExhausted({ CHAT_ALERTS: { fetch } });
      expect(fetch).not.toHaveBeenCalled();
      await expect(
        notifyQuotaExhausted({ CHAT_ALERTS_ENABLED: "true", CHAT_ALERTS: { fetch } }),
      ).resolves.toBeUndefined();
      expect(fetch).toHaveBeenCalledOnce();
      expect(log).toHaveBeenCalledWith(expect.stringContaining("chat.quota_alert_failed"));
    } finally {
      log.mockRestore();
    }
  });
});

function mailer(send, values = new Map()) {
  let queue = Promise.resolve();
  const state = {
    storage: {
      get: async (key) => values.get(key),
      put: async (key, value) => values.set(key, value),
    },
    blockConcurrencyWhile: (task) => {
      const result = queue.then(task);
      queue = result.catch(() => {});
      return result;
    },
  };
  return new DailyQuotaAlerts(state, {
    EMAIL: { send },
    ALERT_FROM: "alerts@example.com",
    ALERT_RECIPIENT: "owner@example.net",
  });
}

describe("daily quota email", () => {
  it("labels setup tests and keeps their deduplication separate from quota alerts", async () => {
    const send = vi.fn(async () => ({}));
    const worker = mailer(send);
    const test = new Request("https://alerts.internal/test", { method: "POST" });
    await worker.fetch(test);
    await worker.fetch(test);
    await worker.fetch(new Request("https://alerts.internal/quota", { method: "POST" }));
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0][0].subject).toContain("delivery test");
    expect(send.mock.calls[1][0].subject).toContain("allowance reached");
  });
  it("allows retrying a failed setup test after fixing verification, with a daily attempt cap", async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error("Not yet verified")).mockResolvedValue({});
    const worker = mailer(send);
    const request = new Request("https://alerts.internal/test", { method: "POST" });
    expect((await worker.fetch(request)).status).toBe(502);
    expect(await (await worker.fetch(request)).json()).toEqual({ status: "sent" });
    expect(await (await worker.fetch(request)).json()).toEqual({ status: "already_attempted" });
    expect(send).toHaveBeenCalledTimes(2);
    const failingSend = vi.fn(async () => {
      throw new Error("Unavailable");
    });
    const failingWorker = mailer(failingSend);
    for (let attempt = 0; attempt < 3; attempt++)
      expect((await failingWorker.fetch(request)).status).toBe(502);
    expect((await failingWorker.fetch(request)).status).toBe(429);
    expect(failingSend).toHaveBeenCalledTimes(3);
  });

  it("exposes a sanitized delivery error code for setup diagnostics", async () => {
    const worker = mailer(async () => {
      throw Object.assign(new Error("Private provider details"), { code: "E_SENDER_NOT_VERIFIED" });
    });
    expect(await (await worker.fetch()).json()).toEqual({
      status: "delivery_failed",
      code: "E_SENDER_NOT_VERIFIED",
    });
  });

  it("serializes concurrent failures and preserves deduplication across restarts", async () => {
    const send = vi.fn(async () => ({ messageId: "sent" }));
    const values = new Map();
    const worker = mailer(send, values);
    await Promise.all(Array.from({ length: 6 }, () => worker.fetch()));
    await mailer(send, values).fetch();
    expect(send).toHaveBeenCalledOnce();
    expect(send.mock.calls[0][0]).toMatchObject({
      to: "owner@example.net",
      from: "alerts@example.com",
    });
  });
  it("does not repeatedly email when delivery fails", async () => {
    const send = vi.fn(async () => {
      throw new Error("Delivery unavailable");
    });
    const worker = mailer(send);
    expect((await worker.fetch()).status).toBe(502);
    expect((await worker.fetch()).status).toBe(200);
    expect(send).toHaveBeenCalledOnce();
  });
  it("permits a new notification on the following UTC day", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-10-28T23:59:59Z"));
      const send = vi.fn(async () => ({}));
      const worker = mailer(send);
      await worker.fetch();
      vi.setSystemTime(new Date("2026-10-29T00:00:01Z"));
      await worker.fetch();
      expect(send).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
