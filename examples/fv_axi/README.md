# Formal run of the qs-ques AXI master, stimulus from its UVM constraints

`axi_fv_top.sv` drives the AXI master (`qs-ques/tutorials/sv_testbench/axi_slave_testbench`) with
`fv_r_mem_req_item`, which fvgen generates from the UVM testbench's `mem_req_item` constraints
(`c_addr`, `c_run`, `c_shape`). The glue is `mem_driver`'s request loop as RTL; the AXI slave
side is unconstrained.

```sh
# 1. extract the UVM testbench with sniper (axi_uvm_tb_constructs.json, generated) and
# 2. turn its constraints into assumes and a randomize() model (fv/)
make fv QS=/path/to/qs-ques UVM=/path/to/uvm-core PYTHON=/path/to/python-with-pyslang
# 3. the qs-ques formal targets
make formal_cover      # 6/6 covers reached (full-line / full-lanes writes, 1-byte read, partial
                       #   AXI strobe, wlast, read issue)
make formal_assert     # PASS to depth 30: every request is a contiguous run inside one line
```

Control: with `#(.CM_c_run(1'b0))` on `u_item` (`constraint_mode(0)` of `c_run`) `formal_assert`
fails `req_in_line` at step 1, i.e. the proof rests on the generated assumptions.

The DUT goes through `read_slang` (yosys-slang): Yosys' `read_verilog` cannot parse
`module axi_master import axi_pkg::*;`. The generated modules and the top go through
`read_verilog -sv -formal -DFORMAL`, as in `questions/shared/make.cfg`.

## The AXI slave side: the testbench's driver as RTL (for review)

`axi_slave_driver_fv.sv` is the UVM testbench's `axi_slave_driver`, together with the responder sequences
that feed it (`axi_rd_resp_seq`, `axi_wr_resp_seq`) and the monitor that turns AXI requests into their
work, written as a synthesizable formal environment. It is a hand translation that follows fixed rules,
listed at the top of the file. Every block names the UVM code it comes from:
- each forked `forever` thread is a clocked state machine;
- `@(posedge clk)` is a state boundary and `vif.x <= v` a register assignment;
- `repeat (n) @(posedge clk)` is a counter state;
- `do @(posedge clk) while (!c)` is a wait state;
- `get_next_item` takes the next request the sequence would hand over;
- `$urandom_range` is a free value assumed in its range;
- `cfg` knobs are parameters.

```sh
make formal_cover_drv      # the covers with the driver model instead of free AXI inputs
```

With free AXI inputs, `formal_cover` reaches all 11 covers, including two the testbench can never
produce: an R beat with no AR issued, and a B response with no write issued (both at step 2). With the
model, `formal_cover_drv` reaches the 9 real ones and proves the 2 spurious ones unreachable to depth 30.

### Does the model do what the driver does? `driver_check/`

```sh
driver_check/record_traces.sh /tmp/axitr          # build the UVM TB with axi_trace_rec.sv bound in; run
                                                  # every test, record the AXI bus per clock edge
driver_check/check_driver.py /tmp/axitr/axi_rd_rand_test.1.trc axi_rd_rand_test
```

For one recorded run, `check_driver.py` builds a formal check:
- the model's DUT-side inputs come from the trace;
- the model's outputs are assumed equal to what the UVM driver drove;
- the model gets that test's `cfg` values as parameters;
- the solver must find choices that produce the whole run (delays, gaps, out-of-order picks, ready bits,
  data).

The verdict is **CONTAINED** when the run is a behaviour of the model. When it isn't, `--where` finds the
first cycle the model cannot follow.

`--mutate` gives controls on the first read burst:
- `rid` (answered with an id never requested) and `rlast` (missing on the last beat) must come out **NOT
  CONTAINED**;
- `rdata` (other data) must stay **CONTAINED**, because memory contents are free in the model.

Results (seed 1, all 9 UVM tests, each passing in simulation):

| test | cycles | verdict |
|---|---:|---|
| axi_smoke_rd_test | 31 | CONTAINED |
| axi_smoke_wr_test | 31 | CONTAINED |
| axi_wr_rand_test | 230 | CONTAINED |
| axi_rd_rand_test | 317 | CONTAINED |
| axi_rw_rand_test | 332 | CONTAINED |
| axi_backpressure_test | 541 | CONTAINED |
| axi_wr_rd_data_test | 583 | CONTAINED |
| axi_stress_test | 873 | CONTAINED |
| axi_ooo_test | 1588 | CONTAINED |

The controls on `axi_smoke_rd_test` give the expected verdicts: `rid` and `rlast` are NOT CONTAINED, and `rdata` is CONTAINED. The short traces take seconds. The longest, `axi_ooo_test`, takes about an hour.

Known limits of the model:
- **Read data is free:** the model does not keep the testbench memory's read-after-write consistency.
- **Queue depth:** the UVM queues are unbounded; `QDEPTH` entries are modelled, and the `drv_queue_fits`
  assertion checks they suffice.
- **Configuration:** one `cfg` per run, given as parameters.

## Coverage closure: the whole UVM test as RTL, short replayable runs per bin

`fvgen --agent <test>` (fvproc.py) turns a UVM test -- both agents, the test's sequence, the monitors -- into one
RTL model: a state machine per testbench process (a state per wait, counters for `repeat (n) @(posedge clk)`,
registers for what a process keeps across edges), every `$urandom_range` / `randomize()` a free value inside
the testbench's own ranges and constraints. `axi_cov_top.sv` puts it around the DUT with a cover per
`axi_coverage` bin (the testbench counts them in string-keyed arrays, so the bins are written out once here),
plus a few deeper events (`env_*`: a read response, a write response, four requests, the DUT's read buffer full).

```
python3 cover_closure.py -o cc_rw --test axi_rw_rand_test            # model, cover search, replay files
python3 cover_closure.py -o cc_bp --test axi_backpressure_test --replay   # ... and replay them in Verilator
python3 cover_closure.py -o cc_jg --test axi_backpressure_test --formal-tool jasper   # Tcl for JasperGold (or vcf)
```

The models are built once for every test (`fvgen --agent-tests`, in `fv_tests/` next to the output): the
constants a test's own code supplies (`run_rand(60, 0)`, `seq.addr = 'h10`) and the cfg fields its `configure()`
sets are parameters, so tests that differ only in those share a model -- here 10 tests are 5 models, the six
`run_rand` tests one (`fv_tests.json`: test -> model, parameter values). `--test` picks the model and values.

- `cc_*/closure.md`: per trace, the bins it reaches and at which edge (all 52 bins within 9-23 cycles)
- `cc_*/fv_replay.sv`: `<class>__fv_replay extends <class>` for every class that makes a random choice (the
  drivers, the sequences, `mem_req_item`), applied by the package itself when `+FV_REPLAY=<trace>.replay` is
  given. Compile it with the testbench and run the test unchanged: `+UVM_TESTNAME=<test> +FV_REPLAY=cov_7.replay`
- `driver_check/replay_check.py` (`--replay`): replays every trace and compares both buses with the formal
  trace at every edge up to the cover. A trace that does not replay is a modelling error, not a result.

The generated models are two-state (`bit`), as the testbench's types: an out-of-range read is 0 in every
tool. yosys-slang gives x there instead, so the yosys flow maps x to 0 (`setundef -zero`) after freeing the
`FV_ANYSEQ` choices. The Jasper / VC Formal scripts free the same choices (`stopat -env` / `snip_driver`) and
follow fvtools.py's backends; they have not been run here (no license).

Results (Verilator, uvm-core 2020): `axi_rw_rand_test` 52/52 bins, `axi_backpressure_test` 52/52 bins plus the
read buffer full at edge 21; every trace replays cycle-exactly. `driver_check/rdbuf_mon.sv` (bound into the
DUT) counts read-buffer-full cycles in simulation: only `axi_backpressure_test` ever fills it (2 of 3 seeds),
because the other tests keep `mem_cpu_ready` high and the buffer drains at once.
