# Tunnel 로직 검토 및 최소 수정 범위

## 검토 범위와 전제

이 문서는 다음과 같은 현재 구현 목적만을 기준으로 한다.

- 한 대의 신뢰된 VPS에서 Caddy와 sshd가 실행된다.
- 서로 다른 여러 서비스를 reverse SSH tunnel로 연결한다.
- 서비스마다 hostname과 SSH remote port를 하나씩 가진다.
- 같은 서비스의 복제본을 동시에 여러 개 실행하는 것은 기본 요구사항이 아니다.
- Docker를 시작, 중지, 재시작했을 때 route가 정확히 등록되고 제거되어야 한다.
- 무중단 blue/green 배포는 필수 요구사항이 아니다.

따라서 별도 중앙 broker, distributed lease, service mesh 같은 구조까지 확장하지 않고 현재 코드에서 실제로 필요한 수정만 정리한다.

## 핵심 불변식

현재 tunnel 로직은 다음 다섯 가지를 보장해야 한다.

```text
1. hostname 하나에는 활성 route가 최대 하나만 존재한다.
2. remote port 하나는 hostname 하나만 가리킨다.
3. app 또는 SSH tunnel 중 하나가 죽으면 컨테이너 전체가 죽는다.
4. 이전 프로세스가 새 프로세스의 route를 삭제할 수 없다.
5. Caddy API 오류를 "route 없음"으로 취급하지 않는다.
```

현재 발견된 주요 lifecycle 문제는 대부분 이 불변식 중 하나를 위반해서 발생한다.

## 반드시 수정해야 하는 tunnel 로직

### 1. 모든 Caddy route 변경을 하나의 lock으로 직렬화한다

현재 claim, reconnect, cleanup, reaper dedupe가 각각 Caddy 설정을 읽고 수정한다. 같은 hostname의 재시작이 겹치면 두 프로세스가 모두 route를 만들거나 이전 프로세스의 cleanup이 새 route를 삭제할 수 있다.

`tunnel.py`와 `tunnel_cleanup.py`가 모두 VPS에서 실행되므로 별도 중앙 broker 없이 공통 file lock 하나를 사용하면 된다.

```text
lock 획득
→ Caddy routes 조회
→ 상태 판단
→ route 삭제 또는 추가
→ 결과 검증
→ lock 해제
```

같은 lock을 다음 작업 전체에 적용해야 한다.

- hostname claim
- reconnect
- cleanup
- reaper reap
- reaper dedupe
- cleanup-all

현재 cleanup이 일부 lock을 사용하지만 모든 mutation이 같은 lock을 사용하지 않기 때문에 경쟁 조건을 막지 못한다. lock 획득에 실패했을 때는 lock 없이 계속 진행하지 말고 작업을 중단한 뒤 다음 health cycle에서 재시도해야 한다.

현재 범위에서 Caddy를 다른 프로그램이 직접 수정하지 않는다면 이 file lock으로 충분하며 DB나 distributed lock은 필요하지 않다.

### 2. hostname claim을 "살아 있으면 거부, 죽었으면 교체"로 바꾼다

현재는 새 tunnel이 같은 hostname의 기존 route를 삭제하고 자신이 가져가려 한다. 같은 hostname을 잘못 설정한 두 서비스가 있으면 마지막에 시작한 서비스가 기존 서비스를 빼앗는다.

최소한 다음 정책을 적용해야 한다.

```text
같은 hostname route 없음
→ claim 허용

같은 hostname route 있음 + 기존 port 살아 있음
→ 새 claim 거부

같은 hostname route 있음 + 기존 port 죽어 있음
→ 기존 route 삭제 후 claim

같은 hostname/port이고 owner도 현재 프로세스
→ reconnect/update 허용
```

무중단 배포가 필요하지 않은 현재 구조에서는 기존 container를 완전히 중지한 다음 새 container를 시작하면 된다.

```text
기존 container stop
→ 기존 SSH port 해제
→ 새 container start
```

### 3. 새 route 등록 시 동일 remote port의 이전 hostname route를 제거한다

이 처리가 없으면 이전 서비스의 stale route와 새 서비스의 port 재사용이 결합해 이전 도메인이 새 서비스로 연결될 수 있다.

예를 들어 다음 상태가 남아 있을 수 있다.

```text
old.example.com → 9011
```

나중에 새 서비스가 9011을 정상적으로 bind해 다음 route를 만들면:

```text
new.example.com → 9011
```

reaper는 9011이 열려 있다는 이유로 이전 route도 살아 있다고 판단할 수 있다. 결과적으로 두 hostname이 모두 새 서비스로 연결된다.

새 tunnel이 remote port를 성공적으로 bind한 후 claim lock 안에서 다음을 수행해야 한다.

```text
현재 port를 가리키는 모든 route 검색
→ 현재 hostname이 아닌 route 삭제
→ 현재 hostname route 등록
```

현재 운영 규모에서는 중앙 port allocator까지 만들 필요 없이 서비스마다 고유 port를 수동 배정하면 충분하다.

```text
한 서비스 = hostname 하나 = remote port 하나
```

여러 hostname alias가 필요한 경우에만 별도 예외 설정을 추가한다.

### 4. Caddy API 조회 실패 시 작업을 중단한다

현재 `_get_all_routes()`는 Caddy API 오류나 예상하지 못한 응답을 빈 route 목록처럼 처리할 수 있다. 그러면 기존 route를 읽지 못한 상태에서 새 route를 추가해 중복이 생긴다.

다음처럼 명확히 구분해야 한다.

```text
200 + 올바른 JSON 배열
→ 정상 처리

특정 ID 조회에서 의미상 허용되는 404
→ route 없음으로 처리

timeout / 500 / invalid JSON / 잘못된 schema
→ 현재 claim 또는 cleanup 중단
→ 다음 cycle에서 재시도
```

또한 `_claim_host()`는 `_delete_tunnels_by_host()`가 실패하면 새 route 추가를 진행하면 안 된다.

### 5. cleanup의 owner 확인과 DELETE를 같은 lock 안에서 수행한다

현재 다음 경쟁 조건이 가능하다.

```text
이전 프로세스: route owner가 자신인 것을 확인
새 프로세스: 같은 route ID를 새 owner로 교체
이전 프로세스: route DELETE
```

이 경우 이전 프로세스가 새 프로세스의 route를 삭제한다.

cleanup 전체를 다음 순서로 실행해야 한다.

```text
lock 획득
→ route 재조회
→ route ID, hostname, port, owner가 모두 현재 프로세스와 같은지 확인
→ 모두 같을 때만 삭제
→ lock 해제
```

`_cleaned_up = True`는 cleanup 시작 시점이 아니라 다음 중 하나가 확인된 뒤에만 설정해야 한다.

- 현재 프로세스의 route를 성공적으로 삭제함
- route가 이미 없음
- route가 다른 owner 소유라 삭제할 필요가 없음

lock 획득이나 Caddy API 호출에 실패했다면 cleanup 완료로 확정하지 않고 재시도 기회를 남겨야 한다.

### 6. health check에서 route의 실제 내용을 검증한다

현재 health check는 route가 존재하고 owner가 같은지를 중심으로 판단한다. hostname이나 upstream port가 잘못되어도 정상으로 판정할 수 있다.

최소한 다음을 비교해야 한다.

- route `@id`
- `group == owner`
- host matcher가 정규화한 hostname과 일치
- reverse proxy port가 기대한 remote port와 일치

앱의 HTTP readiness까지 Caddy route health에서 검사할 필요는 없다. 앱 readiness는 Docker 시작 과정에서 별도로 보장한다.

hostname은 최소한 다음처럼 정규화한다.

```text
lowercase
trailing dot 제거
빈 label 금지
각 label 63자 이하
label 시작과 끝의 '-' 금지
```

현재 국제화 도메인을 사용하지 않는다면 IDNA 처리까지 추가할 필요는 없다.

### 7. HTTP 요청이 reverse proxy route를 먼저 타지 않게 한다

`caddy_config.json`은 하나의 Caddy server가 `:443`과 `:80`을 함께 수신한다. 동적 tunnel route에는 HTTP/HTTPS 구분이 없어 HTTP 요청이 HTTPS redirect보다 먼저 reverse proxy route에 매치될 수 있다.

현재 구조에서는 다음 중 하나로 단순하게 수정한다.

- `sirtunnel` HTTPS server는 `:443`만 listen하고 Caddy가 별도 HTTP redirect server를 만들게 한다.
- 또는 `:80` 전용 server에는 redirect handler만 둔다.

수정 후 다음을 실제 확인해야 한다.

```bash
curl -I http://example.com
# 301 또는 308 redirect

curl -I https://example.com
# 실제 서비스 응답
```

## Reaper에서 필요한 수정

### 8. dry-run에서는 state를 저장하지 않는다

현재 dry-run도 strike count를 state 파일에 기록한다. dry-run을 여러 번 실행한 뒤 실제 reaper를 실행하면 route가 즉시 삭제될 수 있다.

```text
dry-run
→ 계산과 로그만 수행
→ state 파일은 수정하지 않음
```

### 9. route가 0개면 strike state도 비운다

route 목록이 비었을 때 이전 strike가 남으면 동일 ID/port로 재배포된 새 route가 이전 strike를 물려받을 수 있다.

```text
routes == []
→ state = {}
→ state 저장
```

### 10. strike state key에 owner를 포함한다

현재 key는 route ID와 port 중심이라 새 프로세스가 이전 프로세스의 strike를 상속할 수 있다.

```text
현재: route_id:port
수정: route_id:owner:port
```

owner가 바뀌면 새로운 route로 보고 strike를 0부터 시작한다.

### 11. dedupe의 전체 route 배열 덮어쓰기를 제거하거나 공통 lock 안에서만 수행한다

현재 dedupe는 전체 route 배열을 읽고 로컬에서 수정한 뒤 전체 배열을 다시 PATCH한다. 그 사이 다른 tunnel이 route를 추가하면 새 route가 사라질 수 있다.

현재 scope에서는 다음 중 하나면 충분하다.

- 모든 Caddy mutation과 같은 file lock 안에서만 dedupe 실행
- 정상 claim 로직이 hostname/port 유일성을 보장하도록 고친 뒤 자동 dedupe를 제거하고 수동 정리 명령으로만 유지

## Docker lifecycle에서 필요한 수정

### 12. 앱과 autossh의 생명주기를 하나로 묶는다

현재 Dockerfile은 autossh를 background로 실행하고 Bun 앱만 foreground로 둔다.

```text
autossh 종료
→ Bun 앱 계속 실행
→ container는 Up
→ cron 계속 실행
→ public endpoint만 죽음
```

복잡한 supervisor 대신 짧은 entrypoint script 하나로 다음 동작을 구현하면 된다.

```text
Bun 앱 시작
→ localhost:$PORT readiness 확인
→ autossh 시작
→ 두 프로세스 감시

앱 종료
→ autossh 종료
→ container 종료

autossh 종료
→ 앱 종료
→ container 종료

docker stop
→ 앱과 autossh 양쪽에 TERM 전달
→ 둘 다 기다린 후 container 종료
```

Dockerfile의 중첩 `sh -c`는 제거하고 exec-form entrypoint를 사용한다.

```dockerfile
ENTRYPOINT ["/app/docker-entrypoint.sh"]
```

핵심은 앱과 tunnel 중 하나라도 죽으면 컨테이너 전체가 실패해야 한다는 것이다. 그러면 tunnel이 사라진 이전 컨테이너에서 cron만 계속 실행되는 문제도 함께 해결된다.

### 13. 앱 readiness를 확인한 뒤 autossh를 시작한다

현재 Bun 앱과 autossh가 동시에 시작되어 Caddy route가 앱보다 먼저 공개될 수 있다. 이 시간 동안 외부 요청은 502를 받을 수 있다.

필요한 순서는 다음이다.

```text
앱 시작
→ localhost의 실제 HTTP health 확인
→ 성공하면 autossh 시작
```

TCP port open 여부만 확인하지 말고 간단한 `/health` HTTP endpoint 응답을 확인한다.

### 14. 서비스별 SSH remote port를 고유하게 지정한다

각 컨테이너의 로컬 port는 Docker network namespace가 달라 겹쳐도 되지만 VPS의 SSH remote port는 겹치면 안 된다.

예:

```text
kirinnews    9011
service-b    9012
service-c    9013
```

같은 remote port가 지정되면 두 번째 SSH bind가 실패하고, process lifecycle 결합에 따라 두 번째 container도 실패해야 한다. 지금처럼 앱과 cron만 계속 실행되면 안 된다.

### 15. 배포 시 강제 삭제 대신 정상 stop 후 start한다

현재 kirinnews `package.json`의 배포 명령은 `docker container rm -f`를 사용한다.

문제는 다음과 같다.

- 최초 배포에서 기존 container가 없으면 오류가 나 다음 `docker run`이 실행되지 않을 수 있다.
- 강제 삭제라 tunnel cleanup 기회가 없다.
- 새 container 실행 실패 시 기존 서비스가 이미 제거되어 있다.

무중단 배포가 필요하지 않다면 다음 순서면 충분하다.

```text
기존 container가 있으면 docker stop
→ docker rm
→ 새 image build 또는 준비
→ docker run
```

자동 복구가 필요하다면 `--restart unless-stopped`를 추가한다. 운영자가 항상 수동 복구하는 정책이라면 필수는 아니다.

## tunnel 로직 외에 현재 상태에서 필요한 수정

### 16. `.env`와 runtime secret을 Docker image에서 제외한다

현재 Dockerfile은 `SSH_PASSWORD`와 `DOPPLER_TOKEN`을 `ARG`와 `ENV`로 이미지에 저장하고, `.dockerignore` 없이 `COPY . .`를 실행한다.

최소 수정은 다음과 같다.

- `.dockerignore` 추가
- `.env`, `.git`, `node_modules`, 로그와 빌드 결과 제외
- `SSH_PASSWORD`, `DOPPLER_TOKEN`을 Dockerfile `ENV`에서 제거
- runtime 환경변수 또는 secret file로 전달

최소 `.dockerignore` 예시는 다음과 같다.

```dockerignore
.env
.env.*
!.env.example
.git
node_modules
dist
coverage
.DS_Store
*.log
```

기존 Dockerfile로 이미 이미지를 빌드하거나 registry에 올린 적이 있다면 SSH password와 Doppler token을 회전하는 것이 안전하다.

### 17. 공개 cron 실행 endpoint를 제거하거나 인증한다

kirinnews의 `src/index.ts`에는 인증 없이 접근 가능한 다음 endpoint가 있다.

- `GET /clear-cache`
- cron job 목록
- `GET /cron/run/:id`

tunnel을 통해 인터넷에 공개되므로 최소한 다음 중 하나가 필요하다.

- production에서는 해당 route를 등록하지 않음
- 간단한 admin token 검증
- localhost 또는 별도 admin network에서만 노출

현재 구조에서 같은 서비스 container를 항상 하나만 실행하고 앱 또는 tunnel이 죽으면 container 전체가 종료되도록 고친다면 distributed cron lock이나 DB advisory lock까지 추가할 필요는 없다.

### 18. 현재 TypeScript build 오류를 제거한다

`bun x tsc -p tsconfig.json --noEmit`은 `src/cron.ts`의 `noOverlap` 옵션 때문에 실패한다. 설치된 `node-cron` v3의 `ScheduleOptions`에는 해당 옵션이 없다.

실제로 사용하지 않는 `src/cron.ts`라면 제거하거나 tsconfig 대상에서 제외하고, 사용하는 코드라면 현재 `src/tasks/cron.ts`의 process-local 실행 방지 방식으로 통일한다.

Docker build에서도 최소한 typecheck를 한 번 실행해 실행 시점까지 오류가 숨겨지지 않게 한다.

## 수정 후 기대되는 lifecycle

| 상황 | 기대 결과 |
|---|---|
| 서로 다른 hostname과 서로 다른 remote port를 가진 서비스 여러 개 시작 | 각 서비스가 독립적으로 정상 동작 |
| 두 서비스에 같은 remote port 설정 | 두 번째 SSH bind 실패 후 두 번째 container도 실패 |
| 같은 hostname을 두 번째 서비스가 claim | 기존 port가 살아 있으면 새 claim 거부 |
| 정상 `docker stop` | app과 autossh 종료, tunnel cleanup, route 제거 |
| 강제 종료 또는 client 전원 단절 | route가 잠시 남고 port가 닫힌 뒤 reaper가 제거 |
| 같은 서비스 재시작 | 기존 container를 완전히 중지한 뒤 새 container가 동일 host/port claim |
| 앱 crash | autossh도 종료되고 container 실패 |
| autossh 또는 tunnel crash | 앱과 cron도 종료되고 container 실패 |
| stale route의 port를 새 서비스가 재사용 | 새 claim 시 해당 port의 이전 hostname route 제거 |
| Caddy restart | 살아 있는 tunnel이 다음 health cycle에서 route 재등록 |

Caddy restart 시 잠깐 route가 사라지는 것이 허용된다면 `--resume`은 필수가 아니다. 살아 있는 tunnel process를 source of truth로 보고 Caddy가 빈 설정에서 다시 구성되는 정책도 현재 범위에서는 유효하다.

## 현재는 필요하지 않은 확장

현재 요구사항만 보면 다음은 구현하지 않아도 된다.

- 별도 중앙 route broker
- DB 기반 distributed lease
- Kubernetes 또는 service mesh
- 자동 remote port allocator
- blue/green 또는 rolling deploy
- 여러 web replica를 위한 distributed cron lock
- IPv6 대응
- 복잡한 tenant별 Caddy API 권한 체계
- Caddy route autosave 복구를 위한 `--resume`
- 전체 데이터 처리의 대규모 idempotency 재설계

같은 서비스를 여러 container로 동시에 scale하거나 서로 신뢰하지 않는 사용자가 tunnel을 공유해야 하는 요구가 생길 때만 이 항목들을 다시 검토한다.

## 최종 요약

현재 목적에 필요한 핵심 수정은 다음 세 가지로 압축된다.

1. VPS의 모든 Caddy 변경을 하나의 공통 lock으로 묶는다.
2. hostname과 remote port의 1:1 관계를 강제하고 API 오류 시 fail closed한다.
3. 앱과 autossh 중 하나가 죽으면 container 전체가 종료되도록 lifecycle을 결합한다.

여기에 reaper의 dry-run/state 버그, HTTP redirect, Docker secret 포함, 공개 cron endpoint를 수정하면 현재 구현 범위에서 필요한 주요 lifecycle 문제를 해결할 수 있다.
