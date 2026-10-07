from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any

from .collectors import LiveCollectionDisabled
from .domain import (
    CollectionStatus,
    Observation,
    ProcessRecord,
    canonical_system,
    display_system,
    number_matches,
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
    frames: dict[str, tuple[str, ...]] = field(default_factory=dict)
    headless: bool = False

    @classmethod
    def from_mapping(
        cls,
        system: str,
        config: dict[str, Any],
        base_dir: str | Path,
    ) -> PlaywrightCollectorConfig:
        # Sem profile_path, o perfil (cookies da sessão) iria parar na pasta do
        # config.toml; o padrão fica em browser_profiles/, ignorado pelo Git.
        raw_profile = str(config.get("profile_path", "")).strip()
        profile = Path(raw_profile or f"browser_profiles/{canonical_system(system).lower()}")
        if not profile.is_absolute():
            profile = Path(base_dir) / profile
        selectors = {
            str(key): str(value).strip()
            for key, value in config.get("selectors", {}).items()
        }
        frames: dict[str, tuple[str, ...]] = {}
        for key, value in config.get("frames", {}).items():
            chain = (value,) if isinstance(value, str) else tuple(value)
            chain = tuple(str(item).strip() for item in chain if str(item).strip())
            if chain:
                frames[str(key)] = chain
        unknown = sorted(set(frames) - set(selectors))
        if unknown:
            raise ValueError(
                "frames referem seletores inexistentes: " + ", ".join(unknown)
            )
        return cls(
            system=canonical_system(system),
            enabled=bool(config.get("enabled", False)),
            entry_url=str(config.get("entry_url", "")).strip(),
            profile_path=profile,
            selectors=selectors,
            frames=frames,
            headless=bool(config.get("headless", False)),
        )


@dataclass(frozen=True)
class TraceStep:
    """Passo do diagnóstico: só aparece na tela do operador, nunca em banco ou log."""

    step: str
    ok: bool
    detail: str = ""


def _sample(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class PlaywrightCollector:
    """Selector-driven adapter scaffold; never logs in or bypasses security controls."""

    def __init__(self, config: PlaywrightCollectorConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._timeout_error: type[BaseException] | None = None

    def validate_config(self, *, require_collection_selectors: bool = True) -> None:
        if not self.config.enabled:
            raise LiveCollectionDisabled(
                f"Coletor {self.config.system} desativado na configuração."
            )
        missing = ["entry_url"] if not self.config.entry_url else []
        if require_collection_selectors:
            missing.extend(
                key
                for key in REQUIRED_SELECTORS
                if not self.config.selectors.get(key)
            )
        if require_collection_selectors and self.config.selectors.get("detail_link"):
            missing.extend(
                key
                for key in (
                    "result_row",
                    "process_number_cell",
                    "detail_ready",
                )
                if not self.config.selectors.get(key)
            )
        if missing:
            details = ", ".join(missing)
            raise LiveCollectionDisabled(
                f"Configuração incompleta para {self.config.system}: {details}."
            )

    async def start(self, *, require_collection_selectors: bool = True) -> None:
        self.validate_config(
            require_collection_selectors=require_collection_selectors
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
                headless=self.config.headless,
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

    async def is_session_expired(self) -> bool:
        if self._page is None:
            return True
        return await self._is_visible("session_expired")

    async def is_ready(self) -> bool:
        if self._page is None or await self.is_session_expired():
            return False
        if not self.config.selectors.get("process_input", ""):
            return False
        locator = self._locator("process_input").first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            return True
        except self._timeout_error:
            return False

    async def wait_for_manual_login(
        self,
        *,
        require_search_ready: bool = True,
    ) -> bool:
        """Wait for the operator to authenticate in the visible browser window."""
        if self._page is None:
            raise RuntimeError("Inicie o navegador antes do login manual.")
        if require_search_ready and await self.is_ready():
            return False
        import asyncio
        import sys

        if not sys.stdin.isatty():
            raise LiveCollectionDisabled(
                "A sessão precisa de login manual. Execute o comando login "
                "localmente em um terminal interativo."
            )
        await asyncio.to_thread(
            input,
            f"Conclua o login manualmente na janela oficial do "
            f"{display_system(self.config.system)} e pressione Enter. "
            "Não cole credenciais neste terminal.\n",
        )
        await self._page.goto(
            self.config.entry_url,
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        if require_search_ready and not await self.is_ready():
            raise LiveCollectionDisabled(
                "A tela de consulta não ficou disponível após o login. "
                "Verifique a sessão e os seletores autorizados."
            )
        return True

    async def collect(
        self,
        process: ProcessRecord,
        trace: list[TraceStep] | None = None,
    ) -> Observation:
        """Consulta um processo. Com `trace`, registra cada passo para o diagnóstico."""

        def note(step: str, ok: bool, detail: str = "") -> None:
            if trace is not None:
                trace.append(TraceStep(step, ok, detail))

        current = "início"
        if self._page is None:
            raise RuntimeError("Inicie o coletor antes de consultar processos.")
        if process.system != self.config.system:
            raise ValueError("O coletor não corresponde ao sistema do processo.")

        selectors = self.config.selectors
        display_number = ""
        try:
            current = "session_expired (antes da busca)"
            if await self._is_visible("session_expired"):
                note(current, False, "indicador de sessão expirada visível")
                return self._failed(
                    process,
                    CollectionStatus.SESSION_EXPIRED,
                    "SESSAO_EXPIRADA",
                    "Sessão expirada; é necessário login humano autorizado.",
                )
            # Recarrega a busca a cada processo: um resultado da consulta anterior
            # ainda visível faria result_ready passar e os dados de outro processo
            # seriam gravados neste.
            current = "entry_url"
            await self._page.goto(
                self.config.entry_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            note(current, True, "página de busca carregada")
            current = "session_expired"
            if await self._is_visible("session_expired"):
                note(current, False, "indicador de sessão expirada visível")
                return self._failed(
                    process,
                    CollectionStatus.SESSION_EXPIRED,
                    "SESSAO_EXPIRADA",
                    "Sessão expirada; é necessário login humano autorizado.",
                )
            note(current, True, "sessão ativa" if selectors.get("session_expired") else "seletor não configurado")
            current = "process_input"
            await self._locator("process_input").first.wait_for(
                state="visible",
                timeout=30_000,
            )
            await self._locator("process_input").fill(process.number)
            note(current, True, f"número {process.number} digitado")
            current = "search_button"
            await self._locator("search_button").click()
            note(current, True, "busca enviada")
            current = "result_ready"
            try:
                await self._locator("result_ready").first.wait_for(
                    state="visible",
                    timeout=30_000,
                )
                note(current, True, f"{await self._locator('result_ready').count()} elemento(s)")
            except self._timeout_error:
                note(current, False, "não apareceu em 30 s")
                if await self._is_visible("session_expired"):
                    return self._failed(
                        process,
                        CollectionStatus.SESSION_EXPIRED,
                        "SESSAO_EXPIRADA",
                        "Sessão expirada; é necessário login humano autorizado.",
                    )
                if await self._is_visible("no_result"):
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

            current = "no_result"
            if await self._is_visible("no_result"):
                note(current, False, "aviso de processo não localizado visível")
                return self._failed(
                    process,
                    CollectionStatus.NOT_FOUND,
                    "NAO_LOCALIZADO",
                    "O sistema não localizou o processo consultado.",
                )
            note(current, True, "sem aviso de não localizado" if selectors.get("no_result") else "seletor não configurado")

            if selectors.get("detail_link"):
                current = "result_row"
                candidates = self._locator("result_row").filter(
                    has_text=process.number
                )
                candidate_count = await candidates.count()
                if candidate_count == 0:
                    note(current, False, "nenhuma linha contém o número consultado")
                    return self._failed(
                        process,
                        CollectionStatus.EXTRACTION_ERROR,
                        "LINHA_RESULTADO_AUSENTE",
                        "A busca retornou conteúdo, mas não a linha do processo esperado.",
                    )
                rows = []
                seen_numbers: list[str] = []
                for index in range(candidate_count):
                    candidate = candidates.nth(index)
                    number_cell = candidate.locator(
                        selectors["process_number_cell"]
                    )
                    actual_number = " ".join(
                        (await number_cell.inner_text()).split()
                    )
                    if number_matches(process.number, actual_number):
                        rows.append((candidate, actual_number))
                    seen_numbers.append(_sample(actual_number, 60))
                note(
                    "process_number_cell",
                    len(rows) == 1,
                    f"{candidate_count} linha(s) com o número ({'; '.join(seen_numbers)}); "
                    f"{len(rows)} corresponde(m) ao processo",
                )
                if not rows:
                    return self._failed(
                        process,
                        CollectionStatus.EXTRACTION_ERROR,
                        "NUMERO_RESULTADO_DIVERGENTE",
                        "A busca não retornou uma linha com o número consultado.",
                    )
                if len(rows) != 1:
                    return self._failed(
                        process,
                        CollectionStatus.EXTRACTION_ERROR,
                        "RESULTADO_AMBIGUO",
                        "A busca retornou mais de uma linha correspondente ao processo.",
                    )
                display_number = rows[0][1]
                current = "detail_link"
                await rows[0][0].locator(selectors["detail_link"]).click()
                note(current, True, f"detalhe de {display_number} aberto")
                current = "detail_ready"
                try:
                    await self._locator("detail_ready").first.wait_for(
                        state="visible",
                        timeout=30_000,
                    )
                    note(current, True)
                except self._timeout_error:
                    note(current, False, "não apareceu em 30 s")
                    if await self._is_visible("session_expired"):
                        return self._failed(
                            process,
                            CollectionStatus.SESSION_EXPIRED,
                            "SESSAO_EXPIRADA",
                            "Sessão expirada; é necessário login humano autorizado.",
                        )
                    return self._failed(
                        process,
                        CollectionStatus.EXTRACTION_ERROR,
                        "DETALHE_AUSENTE",
                        "A tela de detalhes não apareceu dentro do prazo.",
                    )

            current = "units_open"
            units = tuple(
                unit.strip()
                for unit in await self._locator("units_open").all_inner_texts()
                if unit.strip()
            )
            note(current, bool(units), "; ".join(_sample(unit, 40) for unit in units) or "nenhuma unidade encontrada")
            if not units:
                # Seletor quebrado não pode virar "nenhuma unidade aberta": isso geraria
                # falsa mudança de unidade em todos os processos. Processo concluído,
                # sem unidade aberta, deve ser inativado no Cadastro Mestre.
                return self._failed(
                    process,
                    CollectionStatus.EXTRACTION_ERROR,
                    "UNIDADES_AUSENTES",
                    "Nenhuma unidade aberta foi encontrada; confira o seletor units_open "
                    "ou inative o processo se ele estiver concluído.",
                )
            current = "last_movement"
            last_movement = await self._text("last_movement")
            note(current, bool(last_movement), _sample(last_movement) or "vazio")
            current = "movement_date"
            raw_date = await self._text("movement_date")
            movement_date = _normalize_movement_date(raw_date)
            try:
                parse_movement_date(movement_date)
                date_ok = True
            except ValueError:
                date_ok = False
            note(
                current,
                date_ok,
                f"{_sample(raw_date, 60) or 'vazio'} → {movement_date if date_ok else 'data não reconhecida'}",
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
                display_number=display_number,
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
            note(current, False, "tempo esgotado esperando o seletor")
            return self._failed(
                process,
                CollectionStatus.EXTRACTION_ERROR,
                "TEMPO_LIMITE",
                "A página ou um seletor não respondeu dentro do prazo.",
            )
        except Exception as exc:
            # A mensagem do navegador só vai para o diagnóstico na tela do operador;
            # nunca para banco ou log (pode conter texto da página ou URL).
            note(current, False, f"{type(exc).__name__}: {_sample(str(exc).splitlines()[0] if str(exc) else '', 160)}")
            return self._failed(
                process,
                CollectionStatus.UNAVAILABLE,
                "FALHA_NAVEGADOR",
                f"Falha técnica no navegador ({type(exc).__name__}).",
            )

    def _locator(self, key: str) -> Any:
        """Localiza o seletor configurado, entrando nos iframes indicados em `frames`.

        O SEI exibe árvore e conteúdo do processo em iframes; `frames.<chave>`
        aceita um seletor de iframe ou uma lista, do mais externo ao mais interno.
        """
        scope = self._page
        for frame in self.config.frames.get(key, ()):
            scope = scope.frame_locator(frame)
        return scope.locator(self.config.selectors[key])

    async def _is_visible(self, key: str) -> bool:
        if not self.config.selectors.get(key):
            return False
        locator = self._locator(key).first
        return await locator.count() > 0 and await locator.is_visible()

    async def _text(self, key: str) -> str:
        locator = self._locator(key).first
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
    date_match = re.search(r"(?<!\d)(\d{1,2}/\d{1,2}/\d{4})(?!\d)", cleaned)
    if date_match:
        try:
            return datetime.strptime(date_match.group(1), "%d/%m/%Y").date().isoformat()
        except ValueError:
            pass
    return cleaned

