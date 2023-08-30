from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Type

from docstring_parser import Docstring, DocstringParam, DocstringRaises
from docstring_parser import parse as parse_docstring
from docstring_parser.common import DocstringExample
from frontmatter import parse
from rich import print

from html5tagger import HTML, Builder, Document, E  # type: ignore
from sanic import Request

from .layouts.base import BaseLayout
from .layouts.home import HomeLayout
from .layouts.main import MainLayout
from .markdown import render_markdown, slugify

_PAGE_CACHE: dict[
    str, dict[str, tuple[Page | None, Page | None, Page | None]]
] = {}
_LAYOUTS_CACHE: dict[str, Type[BaseLayout]] = {
    "home": HomeLayout,
    "main": MainLayout,
}
DEFAULT = "en"


@dataclass
class PageMeta:
    title: str = ""
    description: str = ""
    layout: str = "main"


@dataclass
class Page:
    path: Path
    content: str
    meta: PageMeta
    _relative_path: Path | None = None
    next_page: Page | None = None
    previous_page: Page | None = None
    anchors: list[str] = field(default_factory=list)

    def get_layout(self) -> Type[BaseLayout]:
        return _LAYOUTS_CACHE[self.meta.layout]

    @property
    def relative_path(self) -> Path:
        if self._relative_path is None:
            raise RuntimeError("Page not initialized")
        return self._relative_path

    @classmethod
    def get(
        cls, language: str, path: str
    ) -> tuple[Page | None, Page | None, Page | None]:
        if path.endswith("/") or not path:
            path += "index.html"
        if not path.endswith(".md"):
            path = path.removesuffix(".html") + ".md"
        if language == "api":
            path = f"/api/{path}"
        return _PAGE_CACHE.get(language, {}).get(path, (None, None, None))

    @classmethod
    def load_pages(cls, base_path: Path, page_order: list[str]) -> None:
        for path in base_path.glob("**/*.md"):
            relative = path.relative_to(base_path)
            language = relative.parts[0]
            name = "/".join(relative.parts[1:])
            page = cls._load_page(path)
            page._relative_path = relative
            _PAGE_CACHE.setdefault(language, {})[name] = (
                None,
                page,
                None,
            )
            _PAGE_CACHE["api"] = {}
        for language, pages in _PAGE_CACHE.items():
            for name, (_, current, _) in pages.items():
                previous_page = None
                next_page = None
                try:
                    index = page_order.index(name)
                except ValueError:
                    continue
                try:
                    if index > 0:
                        previous_page = pages[page_order[index - 1]][1]
                except KeyError:
                    pass
                try:
                    if index < len(page_order) - 1:
                        next_page = pages[page_order[index + 1]][1]
                except KeyError:
                    pass
                pages[name] = (previous_page, current, next_page)
            previous_page = None
            next_page = None

        api_pages = cls._load_api_pages()
        filtered_order = [ref for ref in page_order if ref in api_pages]
        for idx, ref in enumerate(filtered_order):
            current_page = api_pages[ref]
            previous_page = None
            next_page = None
            try:
                if idx > 0:
                    previous_page = api_pages[filtered_order[idx - 1]]
            except KeyError:
                pass
            try:
                if idx < len(filtered_order) - 1:
                    next_page = api_pages[filtered_order[idx + 1]]
            except KeyError:
                pass
            _PAGE_CACHE["api"][ref] = (previous_page, current_page, next_page)

        for section, items in _PAGE_CACHE.items():
            print(f"[bold yellow]{section}[/bold yellow]")
            for name, (prev, current, next) in items.items():
                print(f"\t[cyan]{name}[/cyan]")

    @staticmethod
    def _load_page(path: Path) -> Page:
        raw = path.read_text()
        metadata, raw_content = parse(raw)
        content = render_markdown(raw_content)
        page = Page(
            path=path,
            content=content,
            meta=PageMeta(**metadata),
        )
        if not page.meta.title:
            page.meta.title = page.path.stem.replace("-", " ").title()

        for line in raw.splitlines():
            if line.startswith("##") and not line.startswith("###"):
                line = line.lstrip("#").strip()
                page.anchors.append(line)

        return page

    @staticmethod
    def _load_api_pages() -> dict[str, Page]:
        docstring_content = _organize_docobjects("sanic")
        output: dict[str, Page] = {}

        for module, content in docstring_content.items():
            path = Path(module)
            page = Page(
                path=path,
                content=content,
                meta=PageMeta(
                    title=path.stem,
                    description="",
                    layout="main",
                ),
            )
            page._relative_path = Path(f"./{module}")
            output[module] = page

        return output


class PageRenderer:
    def __init__(self, base_title: str):
        self.base_title = base_title

    def get_builder(self, full: bool, language: str) -> Builder:
        if full:
            urls = [
                "/assets/code.css",
                "/assets/style.css",
                "/assets/docs.js",
                "https://unpkg.com/htmx.org@1.9.2/dist/htmx.min.js",
                "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js",
            ]
            builder = Document(
                self.base_title, lang=language, _urls=urls, _viewport=True
            )
            builder.full = True
            return builder
        else:
            builder = Builder(name="Partial")
            builder.full = False
            return builder

    def render(self, request: Request, language: str, path: str) -> Builder:
        builder = self.get_builder(
            full=request.headers.get("HX-Request") is None,
            language=language,
        )
        self._body(request, builder, language, path)
        return builder

    def _body(
        self, request: Request, builder: Builder, language: str, path: str
    ):
        prev_page, current_page, next_page = Page.get(language, path)
        request.ctx.language = DEFAULT if language == "api" else language
        request.ctx.current_page = current_page
        request.ctx.previous_page = prev_page
        request.ctx.next_page = next_page
        with self._base(request, builder, current_page):
            if current_page is None:
                builder.h1("Not found")
                return
            builder(HTML(current_page.content))

    @contextmanager
    def _base(self, request: Request, builder: Builder, page: Page | None):
        layout_type: Type[BaseLayout] = (
            page.get_layout() if page else BaseLayout
        )
        layout = layout_type(builder)
        with layout(request, builder.full):
            yield


@dataclass
class DocObject:
    name: str
    module_name: str
    full_name: str
    signature: inspect.Signature | None
    docstring: Docstring
    object_type: str = ""
    methods: list[DocObject] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)


def _extract_classes_methods(obj, full_name, docstrings):
    methods = []
    for method_name, method in inspect.getmembers(obj, is_public_member):
        try:
            signature = _get_method_signature(method)
            docstring = inspect.getdoc(method)
            decorators = _detect_decorators(obj, method)
            methods.append(
                DocObject(
                    name=method_name,
                    module_name="",
                    full_name=f"{full_name}.{method_name}",
                    signature=signature,
                    docstring=parse_docstring(docstring or ""),
                    decorators=decorators,
                    object_type=_get_object_type(method),
                )
            )
        except ValueError:
            pass

    docstrings[full_name].methods = methods


def _get_method_signature(method):
    try:
        return inspect.signature(method)
    except TypeError:
        signature = None
        if func := getattr(method, "fget", None):
            signature = inspect.signature(func)
    return signature


def _extract_docobjects(package_name: str) -> dict[str, DocObject]:
    docstrings = {}
    package = importlib.import_module(package_name)

    for _, name, _ in pkgutil.walk_packages(
        package.__path__, package_name + "."
    ):
        module = importlib.import_module(name)
        for obj_name, obj in inspect.getmembers(module):
            if (
                obj_name.startswith("_")
                or inspect.getmodule(obj) != module
                or not callable(obj)
            ):
                continue
            try:
                signature = inspect.signature(obj)
            except ValueError:
                signature = None
            docstring = inspect.getdoc(obj)
            full_name = f"{name}.{obj_name}"
            docstrings[full_name] = DocObject(
                name=obj_name,
                full_name=full_name,
                module_name=name,
                signature=signature,
                docstring=parse_docstring(docstring or ""),
                object_type=_get_object_type(obj),
            )
            if inspect.isclass(obj):
                _extract_classes_methods(obj, full_name, docstrings)

    return docstrings


def is_public_member(obj: object) -> bool:
    obj_name = getattr(obj, "__name__", "")
    if func := getattr(obj, "fget", None):
        obj_name = getattr(func, "__name__", "")
    return (
        not obj_name.startswith("_")
        and not obj_name.isupper()
        and (
            inspect.ismethod(obj)
            or inspect.isfunction(obj)
            or isinstance(obj, property)
            or isinstance(obj, property)
        )
    )


def _organize_docobjects(package_name: str) -> dict[str, str]:
    page_content: defaultdict[str, str] = defaultdict(str)
    docobjects = _extract_docobjects(package_name)
    for module, docobject in docobjects.items():
        builder = Builder(name="Partial")
        _docobject_to_html(docobject, builder)
        ref = module.rsplit(".", module.count(".") - 1)[0]
        page_content[f"/api/{ref}.md"] += str(builder)
    return page_content


def _render_params(builder: Builder, params: list[DocstringParam]) -> None:
    for param in params:
        with builder.dl(class_="mt-2"):
            dt_args = [param.arg_name]
            if param.type_name:
                parts = [
                    E.br(),
                    E.span(
                        param.type_name,
                        class_="has-text-weight-normal has-text-purple ml-2",
                    ),
                ]
                dt_args.extend(parts)
            builder.dt(*dt_args, class_="is-family-monospace")
            builder.dd(
                HTML(
                    render_markdown(
                        param.description
                        or param.arg_name
                        or param.type_name
                        or ""
                    )
                )
            )


def _render_raises(builder: Builder, raises: list[DocstringRaises]) -> None:
    with builder.div(class_="box mt-5"):
        builder.h5("Raises", class_="is-size-5 has-text-weight-bold")
        for raise_ in raises:
            with builder.dl(class_="mt-2"):
                builder.dt(raise_.type_name, class_="is-family-monospace")
                builder.dd(
                    HTML(
                        render_markdown(
                            raise_.description or raise_.type_name or ""
                        )
                    )
                )


def _render_returns(builder: Builder, docobject: DocObject) -> None:
    assert docobject.docstring.returns
    return_type = docobject.docstring.returns.type_name
    if not return_type or return_type == "None":
        return
    with builder.div(class_="box mt-5"):
        if not return_type and docobject.signature:
            return_type = docobject.signature.return_annotation

        if not return_type or return_type == inspect.Signature.empty:
            return_type = "N/A"

        term = (
            "Return"
            if not docobject.docstring.returns.is_generator
            else "Yields"
        )
        builder.h5(term, class_="is-size-5 has-text-weight-bold")
        with builder.dl(class_="mt-2"):
            builder.dt(return_type, class_="is-family-monospace")
            builder.dd(
                HTML(
                    render_markdown(
                        docobject.docstring.returns.description
                        or docobject.docstring.returns.type_name
                        or ""
                    )
                )
            )


def _render_examples(
    builder: Builder, examples: list[DocstringExample]
) -> None:
    with builder.div(class_="box mt-5"):
        builder.h5("Examples", class_="is-size-5 has-text-weight-bold")
        for example in examples:
            with builder.div(class_="mt-2"):
                builder(
                    HTML(
                        render_markdown(
                            example.description or example.snippet or ""
                        )
                    )
                )


def _define_heading_and_class(
    docobject: DocObject, anchor: Builder, as_method: bool
) -> tuple[str, Builder]:
    anchor_id = slugify(docobject.full_name.replace(".", "-"))
    anchor = E.a("#", class_="anchor", href=f"#{anchor_id}")
    if as_method:
        class_name = "method"
        heading = E.h3(
            docobject.name,
            anchor,
            class_="is-size-4 has-text-weight-bold mt-6",
            id_=anchor_id,
        )
    else:
        class_name = "docobject"
        heading = E.h2(
            E.span(docobject.module_name, class_="has-text-weight-light"),
            ".",
            E.span(docobject.name, class_="has-text-weight-bold is-size-1"),
            anchor,
            class_="is-size-2",
            id_=anchor_id,
        )
    return class_name, heading


def _docobject_to_html(
    docobject: DocObject, builder: Builder, as_method: bool = False
) -> None:
    anchor_id = slugify(docobject.full_name.replace(".", "-"))
    anchor = E.a("#", class_="anchor", href=f"#{anchor_id}")
    class_name, heading = _define_heading_and_class(
        docobject, anchor, as_method
    )

    with builder.div(class_=class_name):
        builder(heading)

        if docobject.docstring.short_description:
            builder.div(
                HTML(render_markdown(docobject.docstring.short_description)),
                class_="short-description mt-3 is-size-5",
            )

        if docobject.object_type == "class":
            mro = [
                item
                for idx, item in enumerate(
                    inspect.getmro(
                        getattr(
                            importlib.import_module(docobject.module_name),
                            docobject.name,
                        )
                    )
                )
                if idx > 0 and item not in (object, type)
            ]
            if mro:
                builder.div(
                    E.span("Inherits from: ", class_="is-italic"),
                    E.span(
                        ", ".join([cls.__name__ for cls in mro]),
                        class_="has-text-weight-bold",
                    ),
                    class_="short-description mt-3 is-size-5",
                )

        builder.p(
            HTML(
                _signature_to_html(
                    docobject.name,
                    docobject.object_type,
                    docobject.signature,
                    docobject.decorators,
                )
            ),
            class_="signature notification is-family-monospace",
        )

        if docobject.docstring.long_description:
            builder.div(
                HTML(render_markdown(docobject.docstring.long_description)),
                class_="long-description mt-3",
            )

        if docobject.docstring.params:
            with builder.div(class_="box mt-5"):
                builder.h5(
                    "Parameters", class_="is-size-5 has-text-weight-bold"
                )
                _render_params(builder, docobject.docstring.params)

        if docobject.docstring.returns:
            _render_returns(builder, docobject)

        if docobject.docstring.raises:
            _render_raises(builder, docobject.docstring.raises)

        if docobject.docstring.examples:
            _render_examples(builder, docobject.docstring.examples)

        for method in docobject.methods:
            _docobject_to_html(method, builder, as_method=True)


def _signature_to_html(
    name: str,
    object_type: str,
    signature: inspect.Signature | None,
    decorators: list[str],
) -> str:
    parts = []
    parts.append("<span class='function-signature'>")
    for decorator in decorators:
        parts.append(
            f"<span class='function-decorator'>@{decorator}</span><br>"
        )
    parts.append(
        f"<span class='is-italic'>{object_type}</span> "
        f"<span class='has-text-weight-bold'>{name}</span>("
    )
    if not signature:
        parts.append("<span class='param-name'>self</span>)")
        parts.append("</span>")
        return "".join(parts)
    for i, param in enumerate(signature.parameters.values()):
        parts.append(f"<span class='param-name'>{escape(param.name)}</span>")
        annotation = ""
        if param.annotation != inspect.Parameter.empty:
            annotation = escape(str(param.annotation))
            parts.append(
                f": <span class='param-annotation'>{annotation}</span>"
            )
        if param.default != inspect.Parameter.empty:
            default = escape(str(param.default))
            if annotation == "str":
                default = f'"{default}"'
            parts.append(f" = <span class='param-default'>{default}</span>")
        if i < len(signature.parameters) - 1:
            parts.append(", ")
    parts.append(")")
    if signature.return_annotation != inspect.Signature.empty:
        return_annotation = escape(str(signature.return_annotation))
        parts.append(
            f": -> <span class='return-annotation'>{return_annotation}</span>"
        )
    parts.append("</span>")
    return "".join(parts)


def _detect_decorators(cls, method):
    decorators = []
    method_name = getattr(method, "__name__", None)
    if isinstance(cls.__dict__.get(method_name), classmethod):
        decorators.append("classmethod")
    if isinstance(cls.__dict__.get(method_name), staticmethod):
        decorators.append("staticmethod")
    if isinstance(method, property):
        decorators.append("property")
    return decorators


def _get_object_type(obj) -> str:
    if inspect.isclass(obj):
        return "class"

    # If the object is a method, get the underlying function
    if inspect.ismethod(obj):
        obj = obj.__func__

    # If the object is a coroutine or a coroutine function
    if inspect.iscoroutine(obj) or inspect.iscoroutinefunction(obj):
        return "async def"

    return "def"
