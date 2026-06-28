---
version: 1
updated: 2026-06-28
status: active
---

# ai-plan — 구현 현황·계획 정본

> **역할.** [`ai-spec`](./ai-spec.md)의 설계가 **(1) 지금 어떤 코드·spec에 구현돼 있는지(현 구현 맵)**와 **(2) 앞으로 무엇을 구현할지(계획)**를 함께 담는다.
> **규칙.** 설계는 복사하지 않고 [`ai-spec`](./ai-spec.md)의 절(§)을 가리킨다. PR 단위 변경 이력은 [`ai-ops`](./ai-ops.md)에 있다.

---

## 1. 목적과 사용법

- **무엇을 보나**: "이 설계가 코드 어디에 있나"(현 구현 맵, §2)와 "앞으로 무엇이 오나"(계획, §3).
- **언제 갱신하나**: ai-spec 설계가 바뀌거나 구현·계획이 진행될 때. 변경의 *기록*은 ai-ops에, 변경의 *대상·기준*은 여기에 둔다.
- **fe/be 접점**: 계획 항목이 다른 레포(server/web/infra)를 건드리면 'fe/be 접점' 열에 표시한다.

## 2. 현 구현 맵 (ai-spec 설계 → 코드)

ai-spec의 각 기능·결정이 manyak-ai 안에서 어디에 구현됐는지. (경로는 레포 루트 기준.)

| ai-spec | 구현 코드 (manyak-ai) | 하위 spec |
|---|---|---|
| **§3.1 스토리라인** | `src/api/v1/story.py` · `services/story_llm.py` · `services/prompt.py` · `schemas/story.py` · `prompt/story/STORYLINES-TEMPLATE.md` | `spec/story/1` |
| **§3.1 컴파일** | `story.py` · `story_llm.py`(`compile_story`) · `story_compile_render.py` · `schemas/story_compile.py` · `prompt/story/COMPILE-TEMPLATE.md` | `spec/story/2` |
| **§3.2 채팅 턴** | `api/v1/chat.py` · `services/chat_assembler.py` · `chat_llm.py` · `chat_next_actions.py` · `schemas/chat_turn.py` · `prompt/chat/*-TEMPLATE.md` | `spec/chat/1~4` |
| **§6 컨텍스트 주입(6레이어)** | `chat_assembler.py`(`assemble`) · `prompt/chat/` | `spec/chat/2·3` |
| **§7 API 계약·메타** | `api/v1/` · `schemas/response_meta.py` · `schemas/*` | — |
| **§8 D1 stateless** | `schemas/chat_turn.py`(session_id 없음) · `chat_assembler.py`(순수 함수) | `spec/chat/4` |
| **§8 D2 하이브리드 컴파일** | `story_compile_render.py`(세분→통글) · `story_llm.py` | `spec/story/2` |
| **§8 D3·D4 본문·선택지 분리 / 3개 보장** | `chat.py` · `chat_next_actions.py` | — |
| **§8 D5 genre 주입** | `story_llm.py`(`_inject_genre`) | — |
| **§8 D6 화자 라벨 정규화** | `chat_llm.py`(`_strip_speaker_bold`) | — |
| **§8 D7 비추론 호출** | `story_llm.py` · `chat_llm.py`(`_THINKING_DISABLED`) | — |
| **§8 D8 프롬프트 버전** | `services/prompt_meta.py`(`read_version`) | — |
| **§8 D9 Sentry·상관관계** | `core/sentry.py` · `middleware.py` · `request_context.py` | — |

## 3. 앞으로의 구현 (계획)

ai-spec 설계 중 아직 구현되지 않았거나 다른 레포 동기화가 필요한 것.

| 항목 | 대상 | 완료 기준 | 근거(ai-spec) | fe/be 접점 |
|---|---|---|---|---|
| **메모리 요약(`summary`) 생성** | be | be가 History를 압축해 `summary`를 만들어 매 턴 전달 | §6.1 · §9 | **be** |
| **web 화자 라벨 파싱** | web(fe) | fe가 `인물명: 대사`를 파싱해 이름표·말풍선으로 표시 | §7.2 | **fe** |
| **키워드북·스탯(향후)** | ai · be | MVP 이후 — World Info 트리거·관계 수치 | §9 | be |

> 위 'be'·'fe' 항목은 다른 레포 관할이라 manyak-ai에서 구현하지 않는다 — ai-spec 계약을 기준으로 각 레포가 맞춘다(접점 변경 시 동기화 필요).

## 4. 의존 순서

- 메모리 요약(be)은 ai가 이미 `summary`를 받을 준비가 돼 있어(D1, `ChatTurnRequest`) **be 단독 진행 가능**.
- web 화자 라벨 파싱은 ai 출력 형식(§7.2)이 확정돼 있어 **fe 단독 진행 가능**.
