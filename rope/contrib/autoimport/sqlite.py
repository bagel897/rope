"""AutoImport module for rope."""
import pathlib
import re
import sqlite3
import sys
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from itertools import chain
from pathlib import Path
from typing import Generator, Iterable, List, Optional, Set, Tuple

from rope.base import exceptions, libutils, resourceobserver, taskhandle
from rope.base.project import Project
from rope.base.resources import Resource
from rope.base.utils import deprecated
from rope.contrib.autoimport.defs import (
    ModuleFile,
    ModuleInfo,
    Name,
    NameType,
    Package,
    PackageType,
    SearchResult,
    Source,
)
from rope.contrib.autoimport.parse import get_names
from rope.contrib.autoimport.utils import (
    get_files,
    get_modname_from_path,
    get_package_tuple,
    sort_and_deduplicate,
    sort_and_deduplicate_tuple,
)
from rope.refactor import importutils


def _get_future_names(
    to_index: List[Tuple[ModuleInfo, Package]],
    underlined: bool,
    job_set: taskhandle.JobSet,
) -> Generator[Future, None, None]:
    """Get all names as futures."""
    with ProcessPoolExecutor() as executor:
        for module, package in to_index:
            job_set.started_job(module.modname)
            yield executor.submit(get_names, module, package)


def filter_packages(
    packages: Iterable[Package], underlined: bool, existing: List[str]
) -> Iterable[Package]:
    """Filter list of packages to parse."""
    if underlined:

        def filter_package(package: Package) -> bool:
            return package.name not in existing

    else:

        def filter_package(package: Package) -> bool:
            return package.name not in existing and not package.name.startswith("_")

    return filter(filter_package, packages)


class AutoImport:
    """A class for finding the module that provides a name.

    This class maintains a cache of global names in python modules.
    Note that this cache is not accurate and might be out of date.

    """

    connection: sqlite3.Connection
    underlined: bool
    rope_project: Project
    project: Package

    def __init__(self, project: Project, observe=True, underlined=False, memory=True):
        """Construct an AutoImport object.

        Parameters
        ___________
        project : rope.base.project.Project
            the project to use for project imports
        observe : bool
            if true, listen for project changes and update the cache.
        underlined : bool
            If `underlined` is `True`, underlined names are cached, too.
        memory : bool
            if true, don't persist to disk
        """
        self.rope_project = project
        project_package = get_package_tuple(
            pathlib.Path(project.root.real_path), project
        )
        assert project_package is not None
        assert project_package.path is not None
        self.project = project_package
        self.underlined = underlined
        db_path: str
        if memory or project.ropefolder is None:
            db_path = ":memory:"
        else:
            db_path = f"{project.ropefolder.path}/autoimport.db"
        self.connection = sqlite3.connect(db_path)
        self._setup_db()
        if observe:
            observer = resourceobserver.ResourceObserver(
                changed=self._changed, moved=self._moved, removed=self._removed
            )
            project.add_observer(observer)

    def _setup_db(self):
        packages_table = "(package TEXT)"
        names_table = (
            "(name TEXT, module TEXT, package TEXT, source INTEGER, type INTEGER)"
        )
        self.connection.execute(f"create table if not exists names{names_table}")
        self.connection.execute(f"create table if not exists packages{packages_table}")
        self.connection.execute("CREATE INDEX IF NOT EXISTS name on names(name)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS module on names(module)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS package on names(package)")
        self.connection.commit()

    @deprecated("Use search or search_full")
    def import_assist(self, starting: str):
        """
        Find modules that have a global name that starts with `starting`.

        For a more complete list, use the search or search_full methods.

        Parameters
        __________
        starting : str
            what all the names should start with
        Return
        __________
        Return a list of ``(name, module)`` tuples
        """
        results = self.connection.execute(
            "select name, module, source from names WHERE name LIKE (?)",
            (starting + "%",),
        ).fetchall()
        return sort_and_deduplicate_tuple(
            results
        )  # Remove duplicates from multiple occurences of the same item

    def search(self, name: str, exact_match: bool = False) -> List[Tuple[str, str]]:
        """
        Search both modules and names for an import string.

        This is a simple wrapper around search_full with basic sorting based on Source.

        Returns a sorted list of import statement, modname pairs
        """
        results: List[Tuple[str, str, int]] = [
            (statement, import_name, source)
            for statement, import_name, source, type in self.search_full(
                name, exact_match
            )
        ]
        return sort_and_deduplicate_tuple(results)

    def search_full(
        self,
        name: str,
        exact_match: bool = False,
        ignored_names: Set[str] = set(),
    ) -> Generator[SearchResult, None, None]:
        """
        Search both modules and names for an import string.

        Parameters
        __________
        name: str
            Name to search for
        exact_match: bool
            If using exact_match, only search for that name.
            Otherwise, search for any name starting with that name.
        ignored_names : Set[str]
            Will ignore any names in this set

        Return
        __________
        Unsorted Generator of SearchResults. Each is guaranteed to be unique.
        """
        results = set(self._search_name(name, exact_match))
        results = results.union(self._search_module(name, exact_match))
        for result in results:
            if result.name not in ignored_names:
                yield result

    def _search_name(
        self, name: str, exact_match: bool = False
    ) -> Generator[SearchResult, None, None]:
        """
        Search both names for avalible imports.

        Returns the import statement, import name, source, and type.
        """
        if not exact_match:
            name = name + "%"  # Makes the query a starts_with query
        for import_name, module, source, name_type in self.connection.execute(
            "SELECT name, module, source, type FROM names WHERE name LIKE (?)", (name,)
        ):
            yield (
                SearchResult(
                    f"from {module} import {import_name}",
                    import_name,
                    source,
                    name_type,
                )
            )

    def _search_module(
        self, name: str, exact_match: bool = False
    ) -> Generator[SearchResult, None, None]:
        """
        Search both modules for avalible imports.

        Returns the import statement, import name, source, and type.
        """
        if not exact_match:
            name = name + "%"  # Makes the query a starts_with query
        for module, source in self.connection.execute(
            "Select module, source FROM names where module LIKE (?)",
            ("%." + name,),
        ):
            parts = module.split(".")
            import_name = parts[-1]
            remaining = parts[0]
            for part in parts[1:-1]:
                remaining += "."
                remaining += part
            yield (
                SearchResult(
                    f"from {remaining} import {import_name}",
                    import_name,
                    source,
                    NameType.Module.value,
                )
            )
        for module, source in self.connection.execute(
            "Select module, source from names where module LIKE (?)", (name,)
        ):
            if "." in module:
                continue
            yield SearchResult(
                f"import {module}", module, source, NameType.Module.value
            )

    @deprecated("Use search or search_full")
    def get_modules(self, name) -> List[str]:
        """Get the list of modules that have global `name`."""
        results = self.connection.execute(
            "SELECT module, source FROM names WHERE name LIKE (?)", (name,)
        ).fetchall()
        return sort_and_deduplicate(results)

    @deprecated("Use search or search_full")
    def get_all_names(self) -> List[str]:
        """Get the list of all cached global names."""
        results = self.connection.execute("select name from names").fetchall()
        return results

    def _dump_all(self) -> Tuple[List[Name], List[Package]]:
        """Dump the entire database."""
        name_results = self.connection.execute("select * from names").fetchall()
        package_results = self.connection.execute("select * from packages").fetchall()
        return name_results, package_results

    def generate_cache(
        self,
        resources: List[Resource] = None,
        underlined: bool = False,
        task_handle=taskhandle.NullTaskHandle(),
    ):
        """Generate global name cache for project files.

        If `resources` is a list of `rope.base.resource.File`, only
        those files are searched; otherwise all python modules in the
        project are cached.
        """

        if resources is None:
            resources = self.rope_project.get_python_files()
        files = [Path(resource.real_path) for resource in resources]
        self._generate_cache(
            files=files, task_handle=task_handle, underlined=underlined
        )

    def generate_modules_cache(
        self,
        modules: List[str] = None,
        task_handle=taskhandle.NullTaskHandle(),
        single_thread: bool = False,
        underlined: bool = False,
    ):
        """
        Generate global name cache for external modules listed in `modules`.

        If no modules are provided, it will generate a cache for every module avalible.
        This method searches in your sys.path and configured python folders.
        Do not use this for generating your own project's internal names,
        use generate_resource_cache for that instead.
        """
        self._generate_cache(
            package_names=modules,
            task_handle=task_handle,
            single_thread=single_thread,
            underlined=underlined,
        )

    # TODO: Update to use Task Handle ABC class
    def _generate_cache(
        self,
        package_names: Optional[List[str]] = None,
        files: Optional[List[Path]] = None,
        underlined: bool = False,
        task_handle=None,
        single_thread: bool = False,
        remove_extras: bool = False,
    ):
        """
        This will work under 3 modes:
        1. packages or files are specified. Autoimport will only index these.
        2. PEP 621 is configured. Only these dependencies are indexed.
        3. Index only standard library modules.
        """
        if self.underlined:
            underlined = True
        if task_handle is None:
            task_handle = taskhandle.NullTaskHandle()
        packages: List[Package] = []
        existing = self._get_existing()
        to_index: List[Tuple[ModuleInfo, Package]] = []
        if files is not None:
            assert package_names is None  # Cannot have both package_names and files.
            for file in files:
                to_index.append((self._path_to_module(file, underlined), self.project))
        else:
            if package_names is None:
                packages = self._get_available_packages()
            else:
                for modname in package_names:
                    package = self._find_package_path(modname)
                    if package is None:
                        continue
                    packages.append(package)
            packages = list(filter_packages(packages, underlined, existing))
            for package in packages:
                for module in get_files(package, underlined):
                    to_index.append((module, package))
            self._add_packages(packages)
        if len(to_index) == 0:
            return
        job_set = task_handle.create_jobset(
            "Generating autoimport cache", len(to_index)
        )
        if single_thread:
            for module, package in to_index:
                job_set.started_job(module.modname)
                for name in get_names(module, package):
                    self._add_name(name)
                    job_set.finished_job()
        else:
            for future_name in as_completed(
                _get_future_names(to_index, underlined, job_set)
            ):
                self._add_names(future_name.result())
                job_set.finished_job()

        self.connection.commit()

    def update_module(self, module: str):
        """Update a module in the cache, or add it if it doesn't exist."""
        self._del_if_exist(module)
        self.generate_modules_cache([module])

    def close(self):
        """Close the autoimport database."""
        self.connection.commit()
        self.connection.close()

    def get_name_locations(self, name):
        """Return a list of ``(resource, lineno)`` tuples."""
        result = []
        modules = self.connection.execute(
            "select module from names where name like (?)", (name,)
        ).fetchall()
        for module in modules:
            try:
                module_name = module[0]
                if module_name.startswith(f"{self.project.name}."):
                    module_name = ".".join(module_name.split("."))
                pymodule = self.rope_project.get_module(module_name)
                if name in pymodule:
                    pyname = pymodule[name]
                    module, lineno = pyname.get_definition_location()
                    if module is not None:
                        resource = module.get_module().get_resource()
                        if resource is not None and lineno is not None:
                            result.append((resource, lineno))
            except exceptions.ModuleNotFoundError:
                pass
        return result

    def clear_cache(self):
        """Clear all entries in global-name cache.

        It might be a good idea to use this function before
        regenerating global names.

        """
        self.connection.execute("drop table names")
        self._setup_db()
        self.connection.commit()

    def find_insertion_line(self, code):
        """Guess at what line the new import should be inserted."""
        match = re.search(r"^(def|class)\s+", code)
        if match is not None:
            code = code[: match.start()]
        try:
            pymodule = libutils.get_string_module(self.rope_project, code)
        except exceptions.ModuleSyntaxError:
            return 1
        testmodname = "__rope_testmodule_rope"
        importinfo = importutils.NormalImport(((testmodname, None),))
        module_imports = importutils.get_module_imports(self.rope_project, pymodule)
        module_imports.add_import(importinfo)
        code = module_imports.get_changed_source()
        offset = code.index(testmodname)
        lineno = code.count("\n", 0, offset) + 1
        return lineno

    def update_resource(self, resource: Resource, underlined: bool = False):
        """Update the cache for global names in `resource`."""
        underlined = underlined if underlined else self.underlined
        path = Path(resource.real_path)
        module = self._path_to_module(path, underlined)
        self._del_if_exist(module_name=module.modname, commit=False)
        self._generate_cache(files=[path], underlined=underlined)

    def _changed(self, resource):
        if not resource.is_folder():
            self.update_resource(resource)

    def _moved(self, resource: Resource, newresource: Resource):
        if not resource.is_folder():
            path = Path(resource.real_path)
            modname = self._path_to_module(path).modname
            self._del_if_exist(modname)
            new_path = Path(newresource.real_path)
            self._generate_cache(files=[new_path])

    def _del_if_exist(self, module_name, commit: bool = True):
        self.connection.execute("delete from names where module = ?", (module_name,))
        if commit:
            self.connection.commit()

    def _get_python_folders(self) -> List[pathlib.Path]:
        folders = self.rope_project.get_python_path_folders()
        folder_paths = [
            pathlib.Path(folder.path) for folder in folders if folder.path != "/usr/bin"
        ]
        return list(OrderedDict.fromkeys(folder_paths))

    def _get_available_packages(self) -> List[Package]:
        packages: List[Package] = [
            Package(module, Source.BUILTIN, None, PackageType.BUILTIN)
            for module in sys.builtin_module_names
        ]
        for folder in self._get_python_folders():
            for package in folder.iterdir():
                package_tuple = get_package_tuple(package, self.rope_project)
                if package_tuple is None:
                    continue
                packages.append(package_tuple)
        return packages

    def _add_packages(self, packages: List[Package]):
        for package in packages:
            self.connection.execute("INSERT into packages values(?)", (package.name,))

    def _get_existing(self) -> List[str]:
        existing: List[str] = list(
            chain(*self.connection.execute("select * from packages").fetchall())
        )
        existing.append(self.project.name)
        return existing

    def _removed(self, resource):
        if not resource.is_folder():
            path = Path(resource.real_path)
            modname = self._path_to_module(path).modname
            self._del_if_exist(modname)

    def _add_future_names(self, names: Future):
        self._add_names(names.result())

    def _add_names(self, names: Iterable[Name]):
        for name in names:
            self._add_name(name)

    def _add_name(self, name: Name):
        self.connection.execute(
            "insert into names values (?,?,?,?,?)",
            (
                name.name,
                name.modname,
                name.package,
                name.source.value,
                name.name_type.value,
            ),
        )

    def _find_package_path(self, target_name: str) -> Optional[Package]:
        if target_name in sys.builtin_module_names:
            return Package(target_name, Source.BUILTIN, None, PackageType.BUILTIN)
        for folder in self._get_python_folders():
            for package in folder.iterdir():
                package_tuple = get_package_tuple(package, self.rope_project)
                if package_tuple is None:
                    continue
                name, source, package_path, package_type = package_tuple
                if name == target_name:
                    return package_tuple

        return None

    def _path_to_module(self, path: Path, underlined: bool = False) -> ModuleFile:
        assert self.project.path
        underlined = underlined if underlined else self.underlined
        # The project doesn't need its name added to the path,
        # since the standard python file layout accounts for that
        # so we set add_package_name to False
        resource_modname: str = get_modname_from_path(
            path, self.project.path, add_package_name=False
        )
        return ModuleFile(
            path,
            resource_modname,
            underlined,
            path.name == "__init__.py",
        )
