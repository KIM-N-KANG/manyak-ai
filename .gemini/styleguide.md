# Styleguide — FastAPI AI Service

이 파일은 Gemini Code Assist가 코드를 제안·리뷰할 때 따르는 팀 규칙이다.

---

## 1. 프로젝트 레이아웃

```
src/
├── api/
│   ├── router.py          # 최상위 라우터 조합
│   └── v1/                # 버전별 엔드포인트
├── core/
│   └── config.py          # pydantic-settings 기반 환경 설정
├── schemas/               # 공유 Pydantic 모델
└── main.py                # FastAPI 앱 진입점
tests/
└── conftest.py            # pytest fixture
```

- `src/` 하위 모듈은 **도메인 → 서비스 → 인프라** 순서로 의존 방향을 유지한다.
- 새 기능은 `src/api/v{n}/` 아래 파일을 추가하고 `api/router.py`에 include한다.
- 라우터 파일 하나에 라우터 인스턴스 하나(`router = APIRouter()`).

---

## 2. 언어·런타임

- Python 3.11+, FastAPI, Pydantic v2, pydantic-settings
- 모든 함수는 `async def`로 선언한다 (I/O 없는 순수 유틸 제외).
- 타입 힌트는 필수. `Any`는 피하고 `TypeAlias` / `TypeVar`를 적극 활용한다.

---

## 3. 코드 스타일

### 네이밍

| 대상 | 규칙 | 예시 |
|------|------|------|
| 변수·함수 | `snake_case` | `get_user_by_id` |
| 클래스 | `PascalCase` | `UserResponse` |
| 상수 | `UPPER_SNAKE_CASE` | `MAX_RETRY_COUNT` |
| 라우터 prefix | `/api/v{n}` | `/api/v1` |
| 엔드포인트 경로 | `kebab-case` | `/user-profile` |

### 임포트 순서

```python
# 1. 표준 라이브러리
from typing import Annotated

# 2. 서드파티
from fastapi import APIRouter, Depends
from pydantic import BaseModel

# 3. 내부 모듈 (src.)
from src.core.config import settings
```

### 응답 모델

- 엔드포인트마다 `response_model=` 을 명시한다.
- 응답 스키마 클래스는 `{도메인}Response`, 요청 바디는 `{도메인}Request` 접미사.

```python
class UserResponse(BaseModel):
    id: int
    email: str

@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int) -> UserResponse:
    ...
```

### 설정

- 환경 변수는 `src/core/config.py`의 `Settings` 클래스에만 선언한다.
- 라우터·서비스에서 직접 `os.environ`을 읽지 않는다.

```python
# Good
from src.core.config import settings
db_url = settings.database_url

# Bad
import os
db_url = os.environ["DATABASE_URL"]
```

---

## 4. 에러 처리

- HTTP 예외는 `fastapi.HTTPException`으로 raise한다.
- 도메인 예외는 커스텀 예외 클래스로 정의하고 `@app.exception_handler`로 변환한다.
- `try/except Exception`으로 모든 예외를 삼키지 않는다.

```python
from fastapi import HTTPException, status

raise HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="User not found",
)
```

---

## 5. 테스트

- 테스트 파일 위치: `tests/`
- 파일명: `test_{모듈명}.py`
- 모든 엔드포인트에 최소 happy-path 테스트 하나.
- `httpx.AsyncClient` + `pytest-asyncio` 사용 (`TestClient` 지양).

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

---

## 6. Git 규칙

### 브랜치 이름

```
{태그}/{KNK-이슈번호}-{작업-제목}

feat/KNK-12-add-chat-endpoint
fix/KNK-20-fix-token-expiry
chore/KNK-30-update-dependencies
```

| 태그 | 기준 |
|------|------|
| `feat` | 새 기능 |
| `fix` | 버그 수정 |
| `docs` | 문서 수정 |
| `refactor` | 기능 변화 없는 구조 개선 |
| `chore` | 설정·의존성·유지보수 |
| `cicd` | 배포·CI/CD |

### 커밋 메시지

```
[KNK-{이슈번호}] {태그}: {커밋 제목}

- 변경 내용 bullet 1
- 변경 내용 bullet 2
```

| 태그 | 기준 |
|------|------|
| `Init` | 초기 생성 |
| `Feat` | 새 기능 |
| `Fix` | 버그 수정 |
| `Docs` | 문서 |
| `Refactor` | 구조 개선 |
| `Chore` | 유지보수 |
| `CICD` | 배포·CI |

### 브랜치 흐름

```
dev → feat/KNK-n-... → dev   (Squash and Merge)
dev → release/vX.Y.Z → main  (Merge Commit)
release → dev                 (Rebase and Merge)
```

- `main`, `dev`에 직접 push 금지.
- `main`, `dev`, `release/*`에 force push 금지.

---

## 7. 금지 패턴

- `print()` 디버깅 — `logging` 모듈 사용.
- 하드코딩된 시크릿·URL — 반드시 `Settings`로 주입.
- `SELECT *` 또는 ORM lazy-load 남발 — 필요한 컬럼만 명시.
- `pass` 만 있는 except 블록.
- 전역 가변 상태 (`global` 키워드).
