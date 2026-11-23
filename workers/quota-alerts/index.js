// Internal service binding only: no public route or workers.dev endpoint.
export default {
  fetch(request, env) {
    if (request.method !== "POST" || !["/quota", "/test"].includes(new URL(request.url).pathname))
      return new Response("Not found", { status: 404 });
    const id = env.DAILY_ALERTS.idFromName("favorite-places-ai-quota");
    return env.DAILY_ALERTS.get(id).fetch(request);
  },
};

export class DailyQuotaAlerts {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const isTest = request && new URL(request.url).pathname === "/test";
    const storageKey = isTest ? "lastSuccessfulTestDay" : "lastAttemptDay";
    return this.state.blockConcurrencyWhile(async () => {
      const day = new Date().toISOString().slice(0, 10);
      if ((await this.state.storage.get(storageKey)) === day)
        return Response.json({ status: "already_attempted" });
      if (isTest) {
        const previous = await this.state.storage.get("testAttempts");
        const attempts = previous?.day === day ? previous.count : 0;
        if (attempts >= 3) return Response.json({ status: "test_attempt_limit" }, { status: 429 });
        await this.state.storage.put("testAttempts", { day, count: attempts + 1 });
      } else {
        // Persist before the external side effect. A crash or ambiguous delivery
        // cannot turn subsequent failed searches into duplicate production alerts.
        await this.state.storage.put(storageKey, day);
      }
      try {
        await this.env.EMAIL.send({
          from: this.env.ALERT_FROM,
          to: this.env.ALERT_RECIPIENT,
          subject: isTest
            ? "Favorites: quota alert delivery test"
            : "Favorites: daily AI search allowance reached",
          text: isTest
            ? "This is a setup test for favorites AI quota alerts. No AI limit has been reached by this test, and no inference was run. Production alerts will be limited to one attempt per UTC day."
            : "The favorites site encountered Cloudflare's daily Workers AI quota limit. Visitors see a friendly message and can still use normal search and filters. The free allocation normally resets at 00:00 UTC. This is the only email attempt for this UTC day. No chat messages or visitor details are included.",
        });
        if (isTest) await this.state.storage.put(storageKey, day);
        console.log(
          JSON.stringify({
            event: isTest ? "chat.quota_alert_test_sent" : "chat.quota_alert_sent",
            day,
          }),
        );
        return Response.json({ status: "sent" });
      } catch (error) {
        const code = /^E_[A-Z_]+$/.test(error?.code) ? error.code : "unknown";
        console.error(
          JSON.stringify({
            event: "chat.quota_alert_delivery_failed",
            day,
            code,
            is_test: Boolean(isTest),
          }),
        );
        return Response.json({ status: "delivery_failed", code }, { status: 502 });
      }
    });
  }
}
