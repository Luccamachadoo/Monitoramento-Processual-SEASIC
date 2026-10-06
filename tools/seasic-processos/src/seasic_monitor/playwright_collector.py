from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Any

from .collectors import LiveCollectionDisabled
from .domain import (
    CollectionStatus,
    Observation,
    ProcessRecord,
    canonical_system,
    parse_movement_date,
    utc_now,
)


REQUIRED_SELECTORS = (
    "process_input",
    "search_button",
    "result_ready",
    "units_open",
    "last_movement",
    "movement_date",
)


@dataclass(frozen=True)
class PlaywrightCollectorConfig:
    system: str
    enabled: bool
    entry_url: str
    profile_path: Path
    selectors: dict[str, str]

    @classmethod
    def from_mapping(
        cls,
        system: str,
        config: dict[str, Any],
        base_dir: str | Path,
    ) -> PlaywrightCollectorConfig:
        profile = Path(config.get("profile_path", ""))
        if not profile.is_absolute():
            profile = Path(base_dir) / profile
        selectors = {
            str(key): str(value).strip()
            for key, value in config.get("selectors", {}).items()
        }
        return cls(
            system=canonical_system(system),
            enabled=bool(config.get("enabled", False)),
            entry_url=str(config.get("entry_url", "")).strip(),
            profile_path=profile,
            selectors=selectors,
        )


class PlaywrightCollector:
    """Selector-driven adapter scaffold; never logs in or bypasses security controls."""

    def __init__(self, config: PlaywrightCollectorConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._timeout_error: type[BaseException] | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            raise LiveCollectionDisabled(
                f"Coletor {self.config.system} desativado na configuração."
            )
        missing = [key for key in REQUIRED_SELECTORS if not self.config.selectors.get(key)]
        if not self.config.entry_url or missing:
            details = ", ".join(missing) if missing else "entry_url"
            raise LiveCollectionDisabled(
                f"Configuração incompleta para {self.config.system}: {details}."
            )
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise LiveCollectionDisabled(
                "Playwright não está instalado neste ambiente. Instale o extra browser "
                "somente no ambiente institucional aprovado."
            ) from exc

        self._timeout_error = PlaywrightTimeoutError
        self.config.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.config.profile_path, 0o700)
        except OSError:
            pass
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_path),
                headless=False,
            )
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            await self._page.goto(
                self.config.entry_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def collect(self, process: ProcessRecord) -> Observation:
        if self._page is None:
            raise RuntimeError("Inicie o coletor antes de consultar processos.")
        if process.system != self.config.system:
            raise ValueError("O coletor não corresponde ao sistema do processo.")

        selectors = self.config.selectors
        try:
            if await self._is_visible(selectors.get("session_expired", "")):
                return self._failed(
                    process,
                    CollectionStatus.SESSION_EXPIRED,
                    "SESSAO_EXPIRADA",
                    "Sessão expirada; é necessário login humano autorizado.",
                )
            await self._page.locator(selectors["process_input"]).fill(process.number)
            await self._page.locator(selectors["search_button"]).click()
            try:
                await self._page.locator(selectors["result_ready"]).wait_for(
                    state="visible",
                    timeout=30_000,
                )
            except self._timeout_error:
                if await self._is_visible(selectors.get("session_expired", "")):
                    return self._failed(
                        process,
                        CollectionStatus.SESSION_EXPIRED,
                        "SESSAO_EXPIRADA",
                        "Sessão expirada; é necessário login humano autorizado.",
                    )
                if await self._is_visible(selectors.get("no_result", "")):
                    return self._failed(
                        process,
                        CollectionStatus.NOT_FOUND,
                        "NAO_LOCALIZADO",
                        "O sistema não localizou o processo consultado.",
                    )
                return self._failed(
                    process,
                    CollectionStatus.EXTRACTION_ERROR,
                    "RESULTADO_AUSENTE",
                    "O seletor do resultado não apareceu dentro do prazo.",
                )

            units = tuple(
                unit.strip()
                for unit in await self._page.locator(selectors["units_open"]).all_inner_texts()
                if unit.strip()
            )
            last_movement = await self._text(selectors["last_movement"])
            movement_date = _normalize_movement_date(
                await self._text(selectors["movement_date"])
            )
            observation = Observation(
                system=process.system,
                number=process.number,
                collected_at=utc_now(),
                status=CollectionStatus.OK,
                units=units,
                executive_sector=units[0] if units else "",
                last_movement=last_movement,
                movement_date=movement_date,
            )
            if not observation.is_valid:
                return self._failed(
                    process,
                    CollectionStatus.EXTRACTION_ERROR,
                    "CAMPOS_MINIMOS_AUSENTES",
                    "Um ou mais campos mínimos não foram extraídos com confiança.",
                )
            return observation
        except self._timeout_error:
            return self._failed(
                process,
                CollectionStatus.EXTRACTION_ERROR,
                "TEMPO_LIMITE",
                "A página ou um seletor não respondeu dentro do prazo.",
            )
        except Exception as exc:
            # Do not store page text, HTML, URLs, or browser exception details in logs.
            return self._failed(
                process,
                CollectionStatus.UNAVAILABLE,
                "FALHA_NAVEGADOR",
                f"Falha técnica no navegador ({type(exc).__name__}).",
            )

    async def _is_visible(self, selector: str) -> bool:
        if not selector:
            return False
        locator = self._page.locator(selector).first
        return await locator.count() > 0 and await locator.is_visible()

    async def _text(self, selector: str) -> str:
        locator = self._page.locator(selector).first
        if await locator.count() == 0:
            return ""
        return (await locator.inner_text()).strip()

    @staticmethod
    def _failed(
        process: ProcessRecord,
        status: CollectionStatus,
        code: str,
        message: str,
    ) -> Observation:
        return Observation(
            system=process.system,
            number=process.number,
            collected_at=utc_now(),
            status=status,
            error_code=code,
            error_message=message,
        )


def _normalize_movement_date(value: str) -> str:
    cleaned = value.strip()
    try:
        return parse_movement_date(cleaned).isoformat()
    except ValueError:
        for date_format in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.strptime(cleaned, date_format).date().isoformat()
            except ValueError:
                continue
    return cleaned

