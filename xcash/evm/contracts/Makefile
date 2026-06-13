FORGE ?= forge
OPENZEPPELIN_CONTRACTS_VERSION := v5.6.1

.PHONY: all deps build test fuzz slither clean fixtures

all: build fixtures

deps:
	@test -f lib/openzeppelin-contracts/package.json || \
		$(FORGE) install OpenZeppelin/openzeppelin-contracts@$(OPENZEPPELIN_CONTRACTS_VERSION) --no-git --shallow
	@python3 -c "import json; version=json.load(open('lib/openzeppelin-contracts/package.json'))['version']; assert version == '$(OPENZEPPELIN_CONTRACTS_VERSION)'[1:], f'openzeppelin-contracts version mismatch: {version}'"

build: deps
	$(FORGE) build --offline
	@mkdir -p artifacts
	@cp out/XcashVaultSlot.sol/XcashVaultSlot.json artifacts/XcashVaultSlot.json
	@python3 -c "import json; d=json.load(open('artifacts/XcashVaultSlot.json')); print(d['bytecode']['object'])" > artifacts/XcashVaultSlot.bin
	@python3 -c "import json; d=json.load(open('artifacts/XcashVaultSlot.json')); print(json.dumps(d['abi'], indent=2, ensure_ascii=False))" > artifacts/XcashVaultSlot.abi.json
	@cp out/XcashVaultSlotFactory.sol/XcashVaultSlotFactory.json artifacts/XcashVaultSlotFactory.json
	@python3 -c "import json; d=json.load(open('artifacts/XcashVaultSlotFactory.json')); print(d['bytecode']['object'])" > artifacts/XcashVaultSlotFactory.bin
	@python3 -c "import json; d=json.load(open('artifacts/XcashVaultSlotFactory.json')); print(json.dumps(d['abi'], indent=2, ensure_ascii=False))" > artifacts/XcashVaultSlotFactory.abi.json

test: deps
	$(FORGE) test --offline -vvv

# 只跑 Fuzz 属性测试（testFuzz_ 前缀），调高 runs 加大随机覆盖。
fuzz: deps
	FOUNDRY_FUZZ_RUNS=10000 $(FORGE) test --offline --match-test testFuzz -vvv

# Slither 静态安全扫描。需先安装：uv tool install slither-analyzer
slither: deps
	@command -v slither >/dev/null 2>&1 || \
		{ echo "slither 未安装：uv tool install slither-analyzer（或 pipx install slither-analyzer）"; exit 1; }
	slither . --config-file slither.config.json

fixtures: deps
	@mkdir -p ../tests/fixtures
	$(FORGE) script scripts/DumpXcashVaultSlotFixtures.s.sol --tc DumpXcashVaultSlotFixtures --offline

clean:
	rm -rf out/ cache/
