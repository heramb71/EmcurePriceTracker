# Architecture Diagrams

Mermaid (`.mmd`) diagrams of the EmcurePriceTracker system, kept in sync with the code.

| File | What it shows |
|------|---------------|
| [architecture.mmd](architecture.mmd) | Services, data sources (yfinance/CoinGecko/Kite), state files, and alert channels |
| [trade-flow.mmd](trade-flow.mmd) | Auto-trade decision + confirm-fill execution and exit management |
| [alerts.mmd](alerts.mmd) | Dual-channel alert dispatch (Telegram primary, WhatsApp best-effort) |
| [daily-lifecycle.mmd](daily-lifecycle.mmd) | Daily timeline for the equity and crypto services (IST) |

## Rendering

- Quick: paste any file into <https://mermaid.live>
- GitHub renders ```mermaid fenced blocks automatically
- CLI (export to SVG/PNG):
  ```bash
  npx @mermaid-js/mermaid-cli -i trade-flow.mmd -o trade-flow.svg
  ```

Keep these updated when the strategy gate, exit rules, channels, or services change.
See [TRADING_WORKFLOW.md](../../TRADING_WORKFLOW.md) for the narrative version.
