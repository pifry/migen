import os
import subprocess
import sys

from migen.fhdl.structure import _Fragment
from migen.build.generic_platform import *
from migen.build import tools
from migen.build.xilinx import common


def _format_constraint(c):
    if isinstance(c, Pins):
        return "LOC=" + c.identifiers[0]
    elif isinstance(c, IOStandard):
        return "IOSTANDARD=" + c.name
    elif isinstance(c, Drive):
        return "DRIVE=" + str(c.strength)
    elif isinstance(c, Misc):
        return c.misc


def _format_ucf(signame, pin, others, resname):
    fmt_c = []
    for c in [Pins(pin)] + others:
        fc = _format_constraint(c)
        if fc is not None:
            fmt_c.append(fc)
    fmt_r = resname[0] + ":" + str(resname[1])
    if resname[2] is not None:
        fmt_r += "." + resname[2]
    return "NET \"" + signame + "\" " + " | ".join(fmt_c) + "; # " + fmt_r + "\n"


def _build_ucf(named_sc, named_pc):
    r = ""
    for sig, pins, others, resname in named_sc:
        if len(pins) > 1:
            for i, p in enumerate(pins):
                r += _format_ucf(sig + "(" + str(i) + ")", p, others, resname)
        else:
            r += _format_ucf(sig, pins[0], others, resname)
    if named_pc:
        r += "\n" + "\n\n".join(named_pc)
    return r


def _build_xst_files(device, sources, vincpaths, build_name, xst_opt):
    prj_contents = ""
    for filename, language, library in sources:
        prj_contents += language + " " + library + " " + tools.cygpath(filename) + "\n"
    tools.write_to_file(build_name + ".prj", prj_contents)

    xst_contents = """run
-ifn {build_name}.prj
-top {build_name}
{xst_opt}
-ofn {build_name}.ngc
-p {device}
""".format(build_name=build_name, xst_opt=xst_opt, device=device)
    if vincpaths:
        xst_contents += "-vlgincdir {"
        for path in vincpaths:
            xst_contents += tools.cygpath(path) + " "
        xst_contents += "}"
    tools.write_to_file(build_name + ".xst", xst_contents)


def _run_yosys(device, sources, vincpaths, build_name):
    ys_contents = ""
    incflags = ""
    for path in vincpaths:
        incflags += " -I" + path
    for filename, language, library in sources:
        ys_contents += "read_{}{} {}\n".format(language, incflags, filename)

    ys_contents += """hierarchy -check -top top
proc; memory; opt; fsm; opt
synth_xilinx -top top -edif {build_name}.edif""".format(build_name=build_name)

    ys_name = build_name + ".ys"
    tools.write_to_file(ys_name, ys_contents)
    r = subprocess.call(["yosys", ys_name])
    if r != 0:
        raise OSError("Subprocess failed")


def _run_ise(build_name, ise_path, source, mode, ngdbuild_opt,
        toolchain, platform, ver=None):
    if sys.platform == "win32" or sys.platform == "cygwin":
        source_cmd = "call "
        script_ext = ".bat"
        shell = ["cmd", "/c"]
        build_script_contents = "@echo off\nrem Autogenerated by Migen\n"
        fail_stmt = " || exit /b"
    else:
        source_cmd = "source "
        script_ext = ".sh"
        shell = ["bash"]
        build_script_contents = "# Autogenerated by Migen\nset -e\n"
        fail_stmt = ""
    if source:
        settings = common.settings(ise_path, ver, "ISE_DS")
        build_script_contents += source_cmd + tools.cygpath(settings) + "\n"
    if mode == "edif":
        ext = "edif"
    else:
        ext = "ngc"
        build_script_contents += """
xst -ifn {build_name}.xst{fail_stmt}
"""

    build_script_contents += """
ngdbuild {ngdbuild_opt} -uc {build_name}.ucf {build_name}.{ext} {build_name}.ngd{fail_stmt}
"""
    if mode == "cpld":
        build_script_contents += """
cpldfit -ofmt verilog {par_opt} -p {device} {build_name}.ngd{fail_stmt}
taengine -f {build_name}.vm6 -detail -iopath -l {build_name}.tim{fail_stmt}
hprep6 -s IEEE1532 -i {build_name}.vm6{fail_stmt}
"""
    else:
        build_script_contents += """
map {map_opt} -o {build_name}_map.ncd {build_name}.ngd {build_name}.pcf{fail_stmt}
par {par_opt} {build_name}_map.ncd {build_name}.ncd {build_name}.pcf{fail_stmt}
bitgen {bitgen_opt} {build_name}.ncd {build_name}.bit{fail_stmt}
"""
    build_script_contents = build_script_contents.format(build_name=build_name,
            ngdbuild_opt=ngdbuild_opt, bitgen_opt=toolchain.bitgen_opt, ext=ext,
            par_opt=toolchain.par_opt, map_opt=toolchain.map_opt,
            device=platform.device, fail_stmt=fail_stmt)
    build_script_contents += toolchain.ise_commands.format(build_name=build_name)
    build_script_file = "build_" + build_name + script_ext
    tools.write_to_file(build_script_file, build_script_contents, force_unix=False)
    command = shell + [build_script_file]
    r = tools.subprocess_call_filtered(command, common.colors)
    if r != 0:
        raise OSError("Subprocess failed")


class XilinxISEToolchain:
    attr_translate = {
        "keep": ("keep", "true"),
        "no_retiming": ("register_balancing", "no"),
        "async_reg": None,
        "mr_ff": None,
        "ars_ff1": None,
        "ars_ff2": None,
        "no_shreg_extract": ("shreg_extract", "no")
    }

    def __init__(self):
        self.xst_opt = """-ifmt MIXED
-use_new_parser yes
-opt_mode SPEED
-register_balancing yes"""
        self.map_opt = "-ol high -w"
        self.par_opt = "-ol high -w"
        self.ngdbuild_opt = ""
        self.bitgen_opt = "-g Binary:Yes -w"
        self.ise_commands = ""

    def build(self, platform, fragment, build_dir="build", build_name="top",
            toolchain_path=None, source=True, run=True, mode="xst", **kwargs):
        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        if toolchain_path is None:
            if sys.platform == "win32":
                toolchain_path = "C:\\Xilinx"
            elif sys.platform == "cygwin":
                toolchain_path = "/cygdrive/c/Xilinx"
            else:
                toolchain_path = "/opt/Xilinx"

        platform.finalize(fragment)
        ngdbuild_opt = self.ngdbuild_opt
        vns = None

        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)
        try:
            if mode in ("xst", "yosys", "cpld"):
                v_output = platform.get_verilog(fragment, name=build_name, **kwargs)
                vns = v_output.ns
                named_sc, named_pc = platform.resolve_signals(vns)
                v_file = build_name + ".v"
                v_output.write(v_file)
                sources = platform.sources | {(v_file, "verilog", "work")}
                if mode in ("xst", "cpld"):
                    _build_xst_files(platform.device, sources, platform.verilog_include_paths, build_name, self.xst_opt)
                    isemode = mode
                else:
                    _run_yosys(platform.device, sources, platform.verilog_include_paths, build_name)
                    isemode = "edif"
                    ngdbuild_opt += "-p " + platform.device

            if mode == "mist":
                from mist import synthesize
                synthesize(fragment, platform.constraint_manager.get_io_signals())

            if mode == "edif" or mode == "mist":
                e_output = platform.get_edif(fragment)
                vns = e_output.ns
                named_sc, named_pc = platform.resolve_signals(vns)
                e_file = build_name + ".edif"
                e_output.write(e_file)
                isemode = "edif"

            tools.write_to_file(build_name + ".ucf", _build_ucf(named_sc, named_pc))
            if run:
                _run_ise(build_name, toolchain_path, source, isemode,
                         ngdbuild_opt, self, platform)
        finally:
            os.chdir(cwd)

        return vns

    # ISE is broken and you must use *separate* TNM_NET objects for period
    # constraints and other constraints otherwise it will be unable to trace
    # them through clock objects like DCM and PLL objects.

    def add_period_constraint(self, platform, clk, period):
        platform.add_platform_command(
            """
NET "{clk}" TNM_NET = "PRD{clk}";
TIMESPEC "TS{clk}" = PERIOD "PRD{clk}" """ + str(period) + """ ns HIGH 50%;
""",
            clk=clk,
            )

    def add_false_path_constraint(self, platform, from_, to):
        platform.add_platform_command(
            """
NET "{from_}" TNM_NET = "TIG{from_}";
NET "{to}" TNM_NET = "TIG{to}";
TIMESPEC "TS{from_}TO{to}" = FROM "TIG{from_}" TO "TIG{to}" TIG;
""",
            from_=from_,
            to=to,
            )
