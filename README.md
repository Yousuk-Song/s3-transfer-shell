# S3 Transfer Shell

로컬 터미널에서 S3 파일을 탐색하고, 내용을 확인하고, 업로드·다운로드하는 도구입니다. 옵션 없이 실행하면 모드 선택 메뉴가 열리며, 탐색 모드에서는 `cd`, `ls`, `less`, `get`, `put` 같은 명령을 사용할 수 있습니다.

![S3 Transfer Shell 실행 메뉴와 로컬 및 S3 파일 흐름](/support/assets/s3-transfer-overview.png)

## GitHub 등록 정보

**Repository name**

```text
s3-transfer-shell
```

**Description**

```text
A menu-driven S3 file manager with an interactive shell, tab completion, previews, bookmarks, and recursive transfers.
```

저장소의 최상위에는 실행 파일만 두고 나머지 파일은 `support/`에 모았습니다. 이 설명서는 GitHub의 `README.md`에서 볼 수 있습니다. 실제 GitHub 저장소 생성이나 업로드는 이 패키지에 포함된 작업이 아닙니다.

## 빠른 시작

```bash
tar -xzf s3_transfer.tar.gz
cd s3_transfer
./s3_transfer
```

이미 설치된 현재 작업 폴더에서는 다음으로 실행합니다.

```bash
cd /Dir/s3_transfer
./s3_transfer
```

```text
  1. S3 탐색
  2. 파일 업로드
  3. 디렉터리 업로드
  4. 파일 다운로드
  5. 디렉터리 다운로드
  6. 이름 검색
  0. 종료
모드 선택:
```

`1`을 입력하면 대화형 탐색으로 들어갑니다. 업로드·다운로드·검색 모드는 경로나 문자열을 이어서 입력합니다. 메뉴의 입력란에서는 공백이 있는 이름도 따옴표 없이 그대로 입력합니다. 빈 경로 입력은 메뉴로 돌아갑니다.

작업 완료 후에는 메뉴로 돌아옵니다. 탐색 중 `exit`를 입력해도 메뉴로 돌아오며, 메뉴에서 `0`을 입력하면 프로그램을 종료합니다. 번호 대신 `shell`, `upload-file`, `upload-dir`, `download-file`, `download-dir`, `search`도 입력할 수 있습니다.

### 어디서든 현재 경로 확인

탐색 셸, 모드 선택, 업로드·다운로드 경로 입력, 검색어 입력, 업로드 `y/n` 확인에서 **`pwd`**를 입력하면 현재 로컬 기준 경로와 S3 경로를 표시합니다. 입력 중이던 작업을 취소하거나 실행하지 않고 같은 입력 단계로 돌아옵니다.

```text
검색 문자열 (빈 입력: 메뉴로): pwd
로컬: /Users/yoos01/songys/test
S3: s3://bucket/transfer/
검색 문자열 (빈 입력: 메뉴로): sample
```

탐색 셸에서는 `lcd`로 바꾼 로컬 경로와 `cd`로 바꾼 S3 경로를 표시합니다. 메뉴에서는 프로그램을 실행한 로컬 경로와 S3 `transfer/` 기준 경로를 표시합니다. 로컬 경로만 필요하면 탐색 셸에서 `lpwd`를 사용하세요. S3 위치와 업로드 기본 목적지는 다를 수 있으며, 업로드 기본 목적지는 항상 `transfer/`입니다.

메뉴에서 이름이나 검색어 자체가 `pwd`이면 `\pwd`를 입력하세요. 파일 경로는 `./pwd`로도 지정할 수 있습니다. 탐색 셸의 `search pwd`는 그대로 이름 검색입니다. 작업 실행 중에는 완료 또는 Ctrl-C 후, `less` 안에서는 `q`로 돌아온 뒤 입력할 수 있습니다.

메뉴를 열지 않고 확인할 수도 있습니다.

```bash
./s3_transfer pwd
./s3_transfer --pwd
```

## 준비 사항

- Python **3.8 이상**, AWS CLI, 해당 S3 경로에 접근 가능한 AWS 인증이 필요합니다. 별도 Python 패키지 설치는 필요하지 않습니다.
- 목록·검색에는 목록 조회 권한, 내용 읽기·다운로드에는 읽기 권한, 업로드에는 쓰기 권한이 필요합니다.
- `less`는 로컬 `less` 프로그램을 사용합니다. Tab 자동완성은 Python의 `readline` 지원이 필요합니다. macOS libedit와 GNU readline을 구분해서 설정합니다.
- 실행 파일 `s3_transfer`와 `support/`를 함께 유지하세요.

```bash
python3 --version
aws --version
less --version

# 이미 설정된 프로파일 사용
AWS_PROFILE=my-profile ./s3_transfer

# 실행 권한 오류가 있는 경우
chmod +x s3_transfer
```

현재 대상은 다음입니다. 다른 저장소를 사용하려면 `support/s3_transfer.py`의 `BUCKET`, `PREFIX`를 변경하세요.

```text
s3://bucket/transfer/
```

이 프로그램은 **S3에 저장된 파일**을 조회합니다. EC2 접속은 필요하지 않으며, EC2 디스크에만 있는 파일은 보이지 않습니다.

## 탐색 모드 사용 예시

아래의 `s3:/transfer...>`는 자동 표시되는 프롬프트입니다. `results`, `summary.txt` 등은 실제 이름으로 바꿔 사용하세요.

```text
s3:/transfer> ls -ltrh
s3:/transfer> cd results
s3:/transfer/results> cat summary.txt
s3:/transfer/results> less run.log
s3:/transfer/results> head -n 20 summary.txt
s3:/transfer/results> tail -n 30 run.log
s3:/transfer/results> lcd ~/Downloads
s3:/transfer/results> get summary.txt
s3:/transfer/results> get -r plots
s3:/transfer/results> bookmark add work
s3:/transfer/results> cd /
s3:/transfer> bookmark go work
s3:/transfer/results> exit
```

탐색 명령의 공백이 있는 이름은 `get "sample 01.bam"`처럼 따옴표로 감싸세요. 셸의 `/`는 **S3 `transfer/` 최상위**를 뜻합니다. `cd ..`를 반복해도 그 밖으로 나가지 않습니다.

## 명령 목록

| 명령 | 기능 |
| --- | --- |
| `pwd` | 현재 로컬 기준 경로와 S3 경로 |
| `cd [경로]`, `cd ..`, `cd /`, `cd -` | S3 경로 이동, 상위·최상위·이전 위치 |
| `ls [-altrh] [경로 또는 패턴]` | S3 목록, 정렬, 크기 표시 |
| `lcd [로컬 경로]` | 이 셸의 로컬 저장·업로드 기준 위치 변경; 생략하면 홈 |
| `lpwd` | 현재 로컬 기준 위치 |
| `lls [-altrh] [로컬 경로]` | 로컬 파일 목록 |
| `cat 파일` | 파일 전체 출력 |
| `less 파일` | 스크롤·검색하며 읽기 |
| `head [-n 줄수] 파일` | 앞부분 읽기; 기본 10줄 |
| `tail [-n 줄수] 파일` | 뒷부분 읽기; 기본 10줄 |
| `get [-r] [-n] [--dry-run] S3경로 [로컬경로]` | 다운로드 |
| `put [-r] [-n] [--dry-run] 로컬경로 [S3경로]` | 업로드 |
| `tree [-L 깊이] [경로]` | 하위 구조; 기본 깊이 2 |
| `du [-h] [경로]` | 하위 전체 파일 수·크기; `-h`는 읽기 쉬운 단위 |
| `stat 파일` | S3 크기·수정 시각·ETag·메타데이터 등 |
| `search 문자열` | `transfer/` 전체에서 이름 부분 문자열 검색 |
| `bookmark add 이름 [경로]` | 현재 또는 지정한 S3 경로를 저장 |
| `bookmark list`, `bookmark go 이름`, `bookmark rm 이름` | 북마크 조회·이동·삭제 |
| `history [개수]` | 이번 셸에서 입력한 명령 표시 |
| `help` | 명령 도움말 |
| `exit`, `quit`, Ctrl-D | 탐색 종료 |

### 로컬 위치와 S3 위치

S3 위치와 로컬 위치는 독립적입니다. `cd`는 S3 위치만, `lcd`는 로컬 기준 위치만 바꿉니다. 로컬 기준 위치는 처음에 프로그램을 실행한 디렉터리이며, 탐색을 종료하면 해당 셸의 위치 설정도 끝납니다.

```text
cd /results
lcd ~/Downloads
lpwd
lls -lh
get report.csv
```

이 예시에서는 `transfer/results/report.csv`를 `~/Downloads/report.csv`로 받습니다. `lcd`는 프로그램을 실행한 바깥 터미널의 작업 디렉터리를 바꾸지 않습니다.

### 목록과 정렬

| 옵션 | 의미 |
| --- | --- |
| `-l` | 크기와 S3 수정 시각 표시 |
| `-t` | 최신 수정 파일부터 정렬 |
| `-r` | 역순; `-tr`은 오래된 파일부터 |
| `-h` | `-l`과 함께 B/K/M/G 등 1024 기준 단위 표시 |
| `-a` | 호환 옵션; 점으로 시작하는 이름은 기본 목록에도 표시 |

```text
ls -ltrh
ls -l -t -r -h
ls -lh /results
ls *.bam
ls -lh results/sample_?.txt
```

목록의 모든 페이지를 모아 정렬합니다. S3 디렉터리는 파일처럼 크기·수정 시각이 있는 객체가 아니므로 `DIR`, `-`로 표시하고, 시간순에서는 파일 뒤에 둡니다.

`ls`와 비재귀 `get`의 패턴은 `*`, `?`, `[]`를 지원합니다. 패턴은 `/`를 넘어 일치하지 않습니다. 예를 들어 `results/*.bam`은 `results/nested/a.bam`에 일치하지 않습니다. 이 도구에서는 따옴표로 감싸도 패턴 문자는 패턴으로 처리합니다. 파이프는 아래의 내용 보기 조합을 지원합니다. 일반 OS 명령 실행·리다이렉션이나 모든 GNU 옵션을 제공하지는 않습니다.

### 내용 미리보기

```text
cat summary.txt
less "summary report.txt"
head -n 20 table.tsv
tail -n 50 run.log
cat wes_cram_estimate.py | less
cat wes_cram_estimate.py | head
cat wes_cram_estimate.py | head -n 20
cat wes_cram_estimate.py | tail -n 30
```

`cat`은 전체 내용을 스트리밍합니다. `less`는 로컬 뷰어에 내용을 전달하며, 현재 디렉터리에 파일을 저장하지 않습니다.

`cat 파일 | less`, `cat 파일 | head [-n 줄수]`, `cat 파일 | tail [-n 줄수]`를 지원합니다. `head`·`tail` 파이프는 필요한 구간만 읽도록 처리합니다. 따옴표 안의 `|`는 파일명으로 처리합니다. 여러 단계의 파이프나 다른 프로그램과의 연결은 지원하지 않으며, 잘못된 명령은 오류를 표시한 뒤 입력으로 돌아옵니다. `less`에서는 `q`로 돌아옵니다.

| less 키 | 동작 |
| --- | --- |
| Space / b | 다음 / 이전 페이지 |
| j / k, 위·아래 화살표 | 한 줄 이동 |
| `/문자열` + Enter | 검색 |
| n / N | 다음 / 반대 방향 검색 결과 |
| g / G | 처음 / 끝으로 이동; G는 남은 데이터를 기다릴 수 있음 |
| q | 해당 다운로드를 중단하고 S3 셸로 복귀 |

`head`·`tail`은 64 KiB 단위의 S3 범위 조회로 필요한 줄을 찾습니다. `tail`은 끝에서 시작 위치를 찾고 필요한 구간을 출력합니다. 아주 긴 줄이나 요청한 줄 수가 파일 전체에 해당하면 많은 데이터를 읽을 수 있습니다. 읽는 중 객체가 바뀌면 조건부 조회가 실패하도록 ETag를 사용합니다. [AWS GetObject 범위·조건부 조회](https://docs.aws.amazon.com/cli/latest/reference/s3api/get-object.html)

미리보기는 UTF-8 텍스트용입니다. 압축 해제·BAM 등 바이너리 해석은 하지 않습니다. `head`·`tail`에서 NUL 바이트를 발견하면 중단하며, 잘못된 UTF-8 바이트는 대체 문자로 표시합니다. 파일을 저장하려면 `get`을 사용하세요.

### 업로드와 다운로드

```text
# 실제 입력할 때는 아래 설명 주석 없이 명령만 입력하세요.
get summary.txt
get summary.txt renamed.txt
get summary.txt ~/Downloads/
get -r results
get -r results ~/Downloads/results-copy
put notes.txt
put notes.txt /results/notes.txt
put -r local_results /results/new_run
```

- 일반 `get`은 로컬 현재 위치에 파일명만 사용해 저장합니다. 선택한 대상이 디렉터리이면 그 안에 저장합니다.
- `get -r results`는 로컬 `results/`를 만들고 그 안에 하위 구조를 유지합니다. 대상을 명시하면 그 디렉터리가 복사된 구조의 루트입니다.
- 업로드 목적지를 생략하면 로컬 상위 경로를 제외하고 **`transfer/` 바로 아래에 파일명·디렉터리명만 사용**합니다. 메뉴, 직접 실행 옵션, 호환 스크립트, 셸의 `put`·`put -r`에 모두 적용됩니다. 셸에서 `cd`로 이동해도 기본 업로드 목적지는 `transfer/`입니다.
- `put /path/to/notes.txt`는 `transfer/notes.txt`, `put -r /path/to/results/`는 `transfer/results/`로 업로드합니다. 디렉터리 내부의 하위 구조는 유지합니다.
- 목적지를 명시하면 그 경로를 사용합니다. 상대 목적지는 현재 S3 위치 기준이며, 파일 목적지가 `/`로 끝나면 파일명을 뒤에 붙입니다. 예: `put notes.txt ./`는 현재 S3 디렉터리에 업로드합니다.
- `put -r local_results /results/new_run`은 디렉터리 **내용**을 `transfer/results/new_run/` 아래에 업로드합니다. 재귀 업로드에서 심볼릭 링크는 건너뜁니다.
- 기존 대상 파일은 덮어쓸 수 있습니다. 원본에 없는 대상 파일을 삭제하지는 않습니다. 빈 디렉터리만 있는 구조는 파일 전송으로 재현되지 않을 수 있습니다.

전송을 실행하기 전에 목록을 확인하거나 기존 파일을 보존할 수 있습니다.

```text
get -r --dry-run results
get -rn results
put -r --dry-run local_results /results/new_run
put -rn local_results /results/new_run
get *.bam ~/Downloads/bam/
get -n *.bam
```

`--dry-run`은 조회와 대상 표시만 수행하며 디렉터리 생성·파일 전송을 하지 않습니다. `-n`은 같은 이름의 대상이 이미 있으면 내용·크기와 관계없이 건너뜁니다. 패턴 다운로드는 파일명만 사용하므로 같은 이름이 서로 다른 경로에 있으면 전송 전에 오류를 내고, 더 좁은 패턴 또는 `get -r` 사용을 안내합니다.

### 구조·용량·메타데이터

```text
tree
tree -L 3 /results
du -h /results
stat /results/summary.txt
```

`tree`는 요청한 깊이까지만 디렉터리를 조회합니다. `du`는 하위 전체 파일을 조회하므로 규모가 크면 시간이 걸립니다. S3 폴더 마커는 파일 수·용량에서 제외합니다. `stat`의 ETag를 파일 MD5 체크섬으로 가정하지 마세요.

### 북마크와 명령 기록

```text
bookmark add results /results
bookmark list
bookmark go results
cd /other
cd -
bookmark rm results
history
history 10
```

북마크는 `support/state/bookmarks.json`에 저장되어 재실행 후에도 유지됩니다. 저장 위치를 바꾸려면 `S3_TRANSFER_STATE_DIR` 환경변수를 지정하세요. 이 상태 디렉터리는 Git과 배포 압축본에서 제외합니다.

`history`는 **이번 탐색 세션**의 명령만 표시합니다. 자동으로 이전 명령을 재실행하지 않습니다.

### Tab 자동완성

명령어나 경로의 앞부분을 입력한 뒤 Tab을 누르세요. 후보가 하나면 완성하고 여러 개면 공통 부분까지 채웁니다. 더 채울 부분이 없을 때 Tab을 다시 누르면 후보 목록이 나옵니다.

```text
cd res<Tab>
less sum<Tab>
head -n 20 sum<Tab>
get -r res<Tab>
lcd ~/Down<Tab>
put local_re<Tab>
get report.txt ~/Down<Tab>
bookmark go res<Tab>
```

`<Tab>`은 실제 Tab 키를 뜻합니다. 명령의 인자에 맞춰 S3 경로 또는 로컬 경로를 제안하며, `cd`·`lcd`·`tree`는 디렉터리만 제안합니다. 공백·따옴표가 포함된 이름도 처리합니다.

**메뉴의 로컬 경로 입력에서도 Tab을 사용할 수 있습니다.** `2. 파일 업로드`에서는 파일과 탐색할 폴더를, `3. 디렉터리 업로드`에서는 폴더만 제안합니다. 상대 경로, 절대 경로, `~/`, `../`를 지원하며 폴더를 완성하면 끝에 `/`가 붙어 이어서 탐색할 수 있습니다.

```text
모드 선택: 2
로컬 경로 (빈 입력: 메뉴로): ~/Down<Tab>

모드 선택: 3
로컬 경로 (빈 입력: 메뉴로): /path/to/res<Tab>
```

메뉴에서는 공백을 포함한 경로도 따옴표나 역슬래시 없이 그대로 입력합니다. Tab도 같은 형식으로 완성합니다. 셸의 `put`, `lcd`, `lls`, `get`의 로컬 목적지 자동완성은 셸 문법에 맞게 공백을 처리합니다. 로컬 후보는 S3에 접속하지 않고 현재 파일 목록에서 조회합니다.

자동완성은 대소문자를 구분하는 앞부분 일치입니다. 이름 중간 문자열은 `search`로 찾으세요. 같은 S3 조회는 최대 5초 재사용하므로 새 파일이 바로 나오지 않으면 잠시 후 다시 Tab을 누르세요.

## 이름 검색과 없는 경로 제안

```text
search sample
search ".bam"
search "sample 01"
```

검색은 현재 S3 위치와 관계없이 `transfer/` 전체가 대상입니다. 파일·디렉터리의 마지막 이름 부분에 문자열이 포함되는지 확인하며 대소문자는 구분하지 않습니다. 파일 내용 검색은 아닙니다.

다운로드·탐색 대상이 없으면 가능한 경우 비슷한 S3 이름을 최대 10개 제안합니다. 제안된 경로로 자동 이동·전송하지 않습니다. 인증·권한 오류는 파일이 없는 경우와 구분합니다.

**업로드할 원본이 없으면 같은 로컬 디렉터리에서 비슷한 이름을 찾고, 가장 비슷한 후보 하나를 업로드할지 확인합니다.** 파일 업로드는 파일끼리, 디렉터리 업로드는 디렉터리끼리 비교합니다. 메뉴·직접 실행·호환 스크립트·셸의 `put`에 적용됩니다.

```text
대상을 찾을 수 없습니다: /data/repotr.txt
가장 비슷한 로컬 파일을 찾았습니다.
  원본: /data/report.txt
  목적지: s3://bucket/transfer/report.txt
이 대상을 업로드할까요? [y/N]: y
```

- `y` 또는 `Y`: 표시한 후보를 업로드합니다. 기본 목적지는 후보의 실제 파일명·디렉터리명을 사용하며, 명시한 목적지가 있으면 그대로 적용합니다.
- `n`, Enter, Ctrl-D, Ctrl-C: 취소하고 입력으로 돌아옵니다. 직접 실행은 종료 코드 1로 끝납니다.
- 그 외 입력: `y` 또는 `n`을 다시 요청합니다.
- `put --dry-run`: 확인 후에도 전송하지 않고 미리보기만 합니다. `-n`의 기존 대상 건너뛰기도 유지됩니다.

로컬 후보 검색은 입력한 원본의 부모 디렉터리만 확인하며, 최대 3,000개 항목과 3초의 검색 예산을 사용합니다. 디스크 전체를 재귀 검색하지 않습니다. 로컬 후보가 없으면 기존처럼 S3 후보를 참고용으로 제안하고 복귀합니다. **S3 항목은 업로드할 로컬 원본을 대신하지 않으므로 업로드 확인을 요청하지 않습니다.**

업로드 후보 확인이 필요한 경우를 제외하면 대상을 찾지 못했을 때 **현재 작업을 끝내고 셸 프롬프트 또는 모드 선택 메뉴로 복귀**합니다. Enter나 `q`를 추가로 누를 필요가 없으며, 없는 파일에 대해 `less`도 열리지 않습니다. 실패한 `cd`는 기존 S3 위치를 유지합니다. 자동 S3 유사 이름 검색은 최대 3초·3페이지(최대 3,000개 객체)로 제한하고, 제한에 도달하거나 조회가 실패해도 복귀합니다. 후보가 없더라도 전체 S3에 비슷한 이름이 없다는 뜻은 아닙니다. 전체 이름 검색은 `search 문자열`을 사용하세요. 이 시간 제한은 최초 대상 확인·명시적 전체 검색·파일 전송에는 적용되지 않습니다.

## 메뉴 없이 직접 실행

```bash
./s3_transfer --help
./s3_transfer --shell
./s3_transfer --upload-file sample.bam
./s3_transfer --upload-dir results
./s3_transfer --upload-file /path/to/sample.bam
./s3_transfer --upload-dir /path/to/results/
./s3_transfer --download-file results/summary.txt
./s3_transfer --download-dir results
./s3_transfer --search "sample" > search_results.tsv
```

