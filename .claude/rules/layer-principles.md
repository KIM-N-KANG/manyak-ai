# 레이어 책임 원칙 (PROMPT 파일 작업 시 적용)

> 이 규칙은 PROMPT/ 파일 작성·수정 시 항상 적용한다.
> 상세 명세는 `Reference/1-PROMPT-LAYER.md`가 SSOT다.

---

## 레이어 우선순위 (충돌 시 이기는 순서)

```
SAFETY > CORE > MEMORY > STORY > CHARACTER > USER
```

우선순위는 배치 순서(물리적 위치)와 다르다. 혼용 금지.

---

## 6레이어 책임 한 줄 요약

| 레이어 | 담당 | 핵심 질문 |
|--------|------|-----------|
| SAFETY | 법률·정책 방어선 | 무엇이 절대 안 되는가 |
| CORE | 출력 형식·메커니즘 | 어떻게 출력하는가 |
| STORY | 서사 무드·전개·세계관 | 어떤 분위기와 흐름인가 |
| CHARACTER | 인물 고정 성향·발화 음색 | 이 인물은 어떻게 말하는가 |
| USER | 플레이어 정적 프로필 | 플레이어는 누구인가 |
| MEMORY | 실시간 동적 상태값 | 지금 무슨 상태인가 |

---

## 절대 원칙 3가지

### 1. 정적 vs 동적 분리
- 정적(Static): SAFETY / CORE / STORY / CHARACTER / USER — 세션 시작 전 고정
- 동적(Dynamic): MEMORY 단독 — 플레이 중 실시간 변경되는 유일한 레이어
- 동적 상태값을 정적 레이어에 쓰면 즉시 MEMORY로 이전한다.

### 2. 가능성 vs 실제 분리 (STORY vs MEMORY)
- STORY: "왕이 암살될 수 있다" 같은 트리거 조건·가능성
- MEMORY: "왕이 실제로 암살당했다" 같은 확정된 결과

### 3. 성향 vs 상태 분리 (CHARACTER vs MEMORY)
- CHARACTER: "원래 타인을 쉽게 믿지 않는다" — 변하지 않는 본성(Trait)
- MEMORY: "사용자를 깊이 신뢰하게 됐다" — 플레이로 변한 관계값(State)

---

## Swap Test (레이어 귀속 판별)

어느 레이어에 넣어야 할지 모호할 때 순서대로 적용:

1. 스토리를 교체해도, 캐릭터를 교체해도 유지되어야 하는가? → **CORE**
2. 스토리를 교체하면 달라지지만, 말하는 인물이 누구든 무관한가? → **STORY**
3. 특정 인물에 따라 달라지는가? → **CHARACTER**

---

## 스타일 책임 3계층

- CORE = Output Style ("어떻게 출력되는가") — 형식·표기 규약
- STORY = Narrative Style ("어떤 분위기·흐름인가") — 무드·묘사 대 대사 비중
- CHARACTER = Speech Style ("이 인물은 어떻게 말하는가") — 어조·말버릇

**묘사 대 대사 비중(분량 배분)의 소유권은 STORY 단독이다.** CORE나 CHARACTER에 쓰지 않는다.

---

## Out of Scope 체크리스트

PROMPT 파일 작성 후 아래 항목을 점검한다:

- [ ] SAFETY에 세계관·캐릭터 설정이 없는가
- [ ] CORE에 서사 무드·전개 속도가 없는가
- [ ] CORE에 묘사 대 대사 비중이 없는가 (→ STORY)
- [ ] STORY에 실시간 플레이 결과가 없는가 (→ MEMORY)
- [ ] CHARACTER에 호감도·신뢰도 수치가 없는가 (→ MEMORY)
- [ ] USER에 플레이 중 변화한 상태가 없는가 (→ MEMORY)
- [ ] MEMORY에 미래 트리거 조건·분기 규칙이 없는가 (→ STORY)
