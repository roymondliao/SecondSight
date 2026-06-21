---
title: "Survey — Official vs Custom Agent Routes for Teams"
status: deprecated
scope: architecture
last_updated: 2026-05-14
summary: "Early survey comparing M365 Agents SDK / Copilot Studio (Route A) vs custom agent runtime + Teams adapter (Route B). Pre-Hermes framing."
superseded_by: docs/system_design_v1.md
deprecated_on: 2026-05-14
deprecation_reason: "Survey-era framing predates the Hermes-Agent decision. Architecture has moved past the Route A/B dichotomy — Hermes covers both shapes."
---

可以，若你的目標是 **Teams 內可被 `@mention` 呼叫、也能定時主動推送** 的「agent employee」，我會把方案分成兩條：**官方 agent 路線** 與 **自建 agent 路線**；兩條都能做到，但重點差在治理、可控性、開發速度與後續擴充自由度。[1][2]
## 架構圖
### 路線 A：官方 agent 路線
這條路線以 **Microsoft 365 Agents SDK / Copilot Studio + Teams** 為主，優勢是 Teams 通道、事件與部署模式比較貼近官方支援；Agents SDK 也明確標示可部署到 Microsoft Teams、Microsoft 365 Copilot、Web 等多個 channel，而且 AI service 本身可自行選擇，不被特定模型綁死。[3][1]

```text
┌──────────────────────────────┐
│ Microsoft Teams Client       │
│ - @mention                   │
│ - channel / group chat       │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Teams App / Agent Manifest   │
│ - install to user/team/chat  │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ M365 Agents SDK or           │
│ Copilot Studio Agent         │
│ - activity handling          │
│ - channel integration        │
│ - adaptive response          │
└──────────────┬───────────────┘
               │
      ┌────────┴─────────┐
      │                  │
      ▼                  ▼
┌───────────────┐   ┌──────────────────┐
│ Agent Runtime │   │ Proactive Msg    │
│ - planner     │   │ Scheduler/Queue  │
│ - tools       │   │ - timer trigger  │
│ - memory      │   │ - save convoId   │
└──────┬────────┘   └────────┬─────────┘
       │                     │
       └──────────┬──────────┘
                  ▼
┌──────────────────────────────┐
│ Azure services               │
│ - Azure OpenAI / Foundry     │
│ - Azure Functions/Container  │
│ - Cosmos DB / PostgreSQL     │
│ - Key Vault / Monitor        │
└──────────────────────────────┘
```

Teams 的 proactive messaging 文件明確要求你保存 `conversationId`，之後才能在沒有使用者先發訊息的情況下主動送通知；這正好對應你第 2 個需求「定時傳送內容到 channel or chat group」。[2]
若你走 Copilot Studio，還能透過 connector / plugin action 去接外部系統，但這條路通常會更依賴 Power Platform 的 connector 與 licensing 模型。[4][5]
### 路線 B：自建 agent 路線
這條路線把 Teams 視為 **前端通道**，你自己的 agent service 才是核心；Teams 收到 mention 或事件後，透過 bot / Teams app 把 activity 送進你的 orchestration 層，再由你自己的 agent loop、tool routing、memory、RAG 與 scheduler 處理。[6][7]

```text
┌──────────────────────────────┐
│ Microsoft Teams Client       │
│ - @mention                   │
│ - channel / group chat       │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Teams Bot / App Adapter      │
│ - receives activities        │
│ - verifies tenant/user       │
│ - stores conversation refs   │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Agent Gateway API            │
│ - auth / rate limit          │
│ - request normalization      │
│ - tool policy / audit        │
└──────────────┬───────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│ Custom Agent Runtime                        │
│ - ADK / PydanticAI / LlamaIndex / Hermes    │
│ - planner / memory / tool calling / RAG     │
│ - human-in-the-loop / approval              │
└──────────────┬──────────────────────────────┘
               │
      ┌────────┼───────────────┬──────────────┐
      ▼        ▼               ▼              ▼
┌─────────┐ ┌──────────┐ ┌─────────────┐ ┌─────────────┐
│ LLM API │ │ VectorDB │ │ SQL / Cache │ │ External APIs│
│ AOAI    │ │ pgvector │ │ Postgres    │ │ Jira/CRM/etc │
└─────────┘ └──────────┘ └─────────────┘ └─────────────┘

               ▲
               │
┌──────────────┴───────────────┐
│ Scheduler / Worker           │
│ - cron / queue / retry       │
│ - proactive send to Teams    │
└──────────────────────────────┘
```

在 Teams 與 Bot/SDK 的訊息流上，使用者訊息不會直接打到你的服務，而是先經由 Teams / Bot channel 再轉進你的 bot endpoint；這也是 Teams bot 架構的標準路徑。[7][6]
而主動發訊部分，不論是舊 Bot 路徑還是新的 Teams SDK proactive messaging，本質都需要保存 conversation reference 或 conversation ID，後續才能由排程或外部 trigger 主動送出訊息。[2][6]
## 推薦技術棧
### 路線 A：官方 agent 路線
如果你希望快速上線、維持 Microsoft 生態一致性，我建議這套：

- Channel / App：Microsoft Teams app + Microsoft 365 Agents SDK。[1]
- Agent tooling：Agents SDK 為殼，AI service 用 Azure OpenAI / Azure AI Foundry。[1]
- Runtime：Node.js 18+ 或 Python 3.9-3.11，因為 Agents SDK 官方支援這些語言版本。[1]
- Proactive：Teams SDK proactive messaging + Azure Functions Timer / Azure Container Apps Jobs。[2]
- State / storage：Azure Cosmos DB 或 PostgreSQL，用來存 user profile、conversationId、job state。[2]
- Secrets / ops：Azure Key Vault、Application Insights / Azure Monitor。

我會偏向 **Node.js + Agents SDK + Azure OpenAI + PostgreSQL**，因為 Teams / Microsoft agent 生態的範例與 integration 常對 JS/TS 友好，而且你若未來要接更多前端式互動與 adaptive cards，TS 開發體驗通常較順。[1][2]
### 路線 B：自建 agent 路線
如果你要高可控、高客製、未來要接內部 workflow、審批、工具鏈，我會建議：

- Teams integration：Teams bot / app adapter + proactive messaging。[6][2]
- Agent runtime：**PydanticAI 或 ADK** 做主流程編排；若 RAG / retrieval 較重，再局部搭配 LlamaIndex。[3]
- LLM：Azure OpenAI 為主，保留切換 OpenAI / Anthropic / 本地模型的 abstraction。
- Backend：Python + FastAPI。
- Queue / scheduler：Azure Functions、Azure Container Apps Jobs，或 Celery + Redis。
- DB：PostgreSQL + pgvector；快取用 Redis。
- Observability：OpenTelemetry + Application Insights / Grafana。
- Identity / policy：Microsoft Entra ID、Graph API、內部 RBAC。

若你想接 OpenClaw 或 Hermes，我會把它們放在 **Custom Agent Runtime** 這層，而不是讓它們直接面對 Teams；Teams 端還是保留一個穩定的 adapter / gateway，這樣未來替換 agent engine 成本最低。[3][6]
## 優缺點
### 路線 A：官方 agent 路線
優點：
- 和 Teams / Microsoft 365 channel 的整合阻力較低，官方已提供 channel scaffolding 與 activity/event handling 能力。[1]
- 上線速度通常較快，對 `@mention`、安裝、通道分發、proactive messaging 的支援路徑較明確。[2][1]
- 對企業 IT / admin 接受度通常較高，治理與權限模型更貼近 Microsoft 生態。[8][4]

缺點：
- Agents SDK 目前仍是 **preview**，文件也標明 prerelease，代表 API 與實作面未必完全穩定。[1]
- 若走 Copilot Studio，容易碰到 connector、quota、licensing、Dataverse 限制；例如 Teams app limits、topics、skills 與 payload 都有上限。[4]
- 進階 agent loop、複雜 tool policy、細緻 observability、跨模型 routing，通常不如自建靈活。[5][1]
### 路線 B：自建 agent 路線
優點：
- 你可以完全掌控 planner、memory、tool use、approval、multi-agent、RAG 與 fallback 策略，最適合複雜企業流程。[3]
- 比較容易把現有 Python agent stack、資料系統與內部服務整合成一致平台。
- 框架可替換，今天用 PydanticAI / ADK，明天換 Hermes / OpenClaw 也只要改 runtime 層，不用重做 Teams 入口。

缺點：
- Teams 整合的 plumbing 要自己處理，像 app manifest、bot endpoint、conversation reference、主動推播路徑都要自己設計與維護。[6][2]
- 安全、權限、審計、重試、訊息格式與 observability 的責任更多，平台工程負擔較高。
- 開發週期會比官方 route 長，特別是在第一次打通 Teams mention + proactive messaging + app installation 時。
## 我會怎麼選
如果你是要先做 **MVP / 內部 PoC**，而且目標是盡快在 Teams 裡跑起來，我會先選 **路線 A：Agents SDK 為主**，因為它同時保留一定 AI stack 自由度，又比純 Copilot Studio 更適合工程團隊控制後端能力。[1]
如果你是要做 **長期可演化的 agent employee 平台**，而且會接內部知識、流程審批、任務代理、多工具執行，我會選 **路線 B：自建 agent runtime + Teams adapter**，把 Teams 視為一個 channel，不把核心能力綁死在單一廠商抽象層上。[6][3]
## 實務建議
對你這個場景，我最推薦的落地策略是：

- 第 1 階段：用 **Teams adapter + 自建 Python agent runtime** 做核心。[6]
- 第 2 階段：保留與 **Microsoft 365 Agents SDK** 相容的 channel abstraction，必要時再補官方 agent route。[1]
- 第 3 階段：把 `@mention`、scheduled digest、approval workflow、tool audit log 做成統一事件流與狀態機。

這樣的原因是：你的需求不只是聊天，而是 **agent employee**，通常會往 workflow、memory、approval、system integration 演進；這種情境下，自建 runtime 的中長期性價比通常更高，而 Teams 只是最先接上的工作入口。[3][2]

你要的話，我下一則可以直接補一版更工程化的內容：
**1. 兩條路線的 deployment diagram**、
**2. 資料表設計（conversation / job / memory / tool_call）**、
**3. 一份推薦的 monorepo 結構。**

Sources
[1] Send chatMessage in a channel or a chat - Microsoft Graph ... https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0
[2] Working with Microsoft Teams messaging APIs in ... https://learn.microsoft.com/en-us/graph/teams-messaging-overview
[3] 什麼是Microsoft 365 代理程式SDK https://learn.microsoft.com/zh-tw/microsoft-365/agents-sdk/agents-sdk-overview
[4] Quotas and limits for Copilot Studio https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-quotas
[5] Use connectors in Copilot Studio agents https://learn.microsoft.com/en-us/microsoft-copilot-studio/advanced-connectors
[6] Send proactive messages - Teams https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages
[7] Azure Bot Service & Microsoft Teams - Reverse Engineering https://moimhossain.com/2025/05/22/azure-bot-service-microsoft-teams-architecture-and-message-flow/
[8] Connect and configure an agent for Teams and Microsoft 365 https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-add-bot-to-microsoft-teams
[9] Proactive Messaging https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/essentials/sending-messages/proactive-messaging
[10] 傳送主動式通知給使用者- Bot Service https://learn.microsoft.com/zh-tw/azure/bot-service/bot-builder-howto-proactive-message?view=azure-bot-service-4.0
[11] Proactive messaging from external trigger (Postman) using ... https://github.com/OfficeDev/microsoft-365-agents-toolkit/discussions/14931
[12] Part 4 - Reaching Users Everywhere with M365 Agents https://spknowledge.com/2026/02/23/part-4-multi-channel-deployment-reaching-users-everywhere-with-m365-agents/
[13] Copilot Studio Limitations - What It Cannot Do Yet https://team400.ai/blog/2026-04-11-copilot-studio-limitations
[14] Sending Proactive Messages with Microsoft 365 Agents SDK https://zenn.dev/karamem0/articles/2026_01_20_180000?locale=en
[15] GitHub - Mimetis/microsoft_teams_bot_proactive_message_authentication_graph: Creating a Bot within Microsoft Teams, then sending a proactive message, then authenticate the user to send eventually an email ! https://github.com/Mimetis/microsoft_teams_bot_proactive_message_authentication_graph
[16] Sending proactive messages from an outside process to ... https://stackoverflow.com/questions/71547766/sending-proactive-messages-from-an-outside-process-to-organizational-users-via-t
[17] TeamsProactiveServiceEndpoints Class (Microsoft.Agents. ... https://learn.microsoft.com/en-us/dotnet/api/microsoft.agents.extensions.teams.teamsproactiveserviceendpoints?view=m365-agents-sdk
[18] Send proactive notifications to users - Bot Service https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-howto-proactive-message?view=azure-bot-service-4.0
