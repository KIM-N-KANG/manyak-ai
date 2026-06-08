# 프로젝트 규칙

## 필수 참고 위키
모든 작업 전에 반드시 아래 경로의 위키를 읽고 시작한다.

위키 경로: C:\Users\dohyeong0423\Desktop\asm\Project\llm-wiki

읽어야 할 파일:
- index.md — 전체 페이지 목록
- wiki/concepts/ — 팀 컨벤션 및 규칙
- wiki/entities/ — 기술 스택 및 도구

---

## 프로젝트 개요

스토리 진행형 AI 캐릭터 서비스의 시스템 프롬프트 아키텍처를 설계·구현하는 프로젝트.
사용자는 작가가 설계한 세계관과 시나리오 안에서 AI 캐릭터와 상호작용하며 이야기를 진행한다.

## 폴더 구조

```
AI/
├── PROMPT/       ← 실제 프롬프트 구현 (작업 대상)
├── Reference/    ← 설계 명세서 (SSOT, 수정 금지)
└── .claude/
    └── rules/    ← 레이어 작업 시 자동 로드되는 원칙
```

## SSOT (단일 진실 공급원)

- 레이어 책임 정의 → `Reference/1-PROMPT-LAYER.md`
- 레이어 배치 규칙 → `Reference/2-LAYER-PLACEMENT.md`
- 프롬프트 조립 원리 → `Reference/3-CONTEXT-ARCHITECTURE.md`

Reference/ 파일과 내 지시가 충돌하면 Reference/가 우선한다.

## PROMPT 파일 작업 규칙

1. 작업 전 반드시 `.claude/rules/layer-principles.md`를 읽는다.
2. 레이어 간 책임 중복 금지 — 모호하면 Swap Test로 판별한다.
3. 정적 정보(설정·규칙)와 동적 정보(상태값)를 절대 같은 레이어에 쓰지 않는다.
4. 각 레이어 파일은 Definition / Objective / Responsibilities / Scope / Relationship 순서로 기술한다.
5. 구현 전 완료 기준(어떤 내용이 포함되고 무엇이 없어야 하는지)을 먼저 합의한다.