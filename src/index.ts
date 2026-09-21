import { Container, getContainer } from "@cloudflare/containers";
import type { StopParams } from "@cloudflare/containers";

/**
 * Durable Object that manages the always-running Telegram bot container.
 *
 * A cron trigger fires every minute and calls keepAlive() (RPC), which
 * starts the container if it crashed and renews the activity timeout so
 * it never sleeps. The Worker's secrets are injected into the container
 * environment at start time.
 */
export class BotContainer extends Container {
  defaultPort = 8080;
  // Safety net: if the cron keep-alive ever stops, the container sleeps
  // after 5 idle minutes and the next cron tick restarts it.
  sleepAfter = "5m";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    this.envVars = {
      API_ID: String(env.API_ID ?? ""),
      API_HASH: env.API_HASH ?? "",
      BOT_TOKEN: env.BOT_TOKEN ?? "",
      PHONE: env.PHONE ?? "",
      ADMIN_IDS: env.ADMIN_IDS ?? "",
      USER_STRING_SESSION: env.USER_STRING_SESSION ?? "",
      DB_PATH: "/app/data/bot.db",
    };
  }

  override onStart(): void {
    console.log("[bot] container started");
  }

  override onStop({ exitCode, reason }: StopParams): void {
    console.log("[bot] container stopped", { exitCode, reason });
  }

  override onError(reason: unknown): void {
    console.error("[bot] container error:", reason);
  }

  /** RPC method called by the cron trigger every minute. */
  async keepAlive(): Promise<void> {
    // start() is a no-op when the container is already running.
    await this.start();
    // Reset the idle timer so the container never sleeps.
    this.renewActivityTimeout();
  }
}

export interface Env {
  BOT_CONTAINER: DurableObjectNamespace<BotContainer>;
  API_ID?: string;
  API_HASH?: string;
  BOT_TOKEN?: string;
  PHONE?: string;
  ADMIN_IDS?: string;
  USER_STRING_SESSION?: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      const container = getContainer(env.BOT_CONTAINER, "bot");
      await container.keepAlive();
      try {
        return await container.fetch(new Request("http://container/health"));
      } catch {
        return Response.json({ status: "starting" }, { status: 503 });
      }
    }

    return Response.json({
      bot: "telegram-forward-bot",
      status: "running",
      health: "GET /health",
    });
  },

  // Keep the container alive 24/7.
  async scheduled(
    _controller: ScheduledController,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<void> {
    ctx.waitUntil(getContainer(env.BOT_CONTAINER, "bot").keepAlive());
  },
} satisfies ExportedHandler<Env>;
