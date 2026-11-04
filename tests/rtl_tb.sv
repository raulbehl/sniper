// Testbench for test_fvgen.py T5: covergroups sampled from an interface signal driven by a DUT port,
// an interface signal tapped from a DUT-internal net, a register-model mirror (HDL path), and a
// testbench-only value. The uvm_reg* classes are stand-ins with the UVM method signatures.
interface rf_if (input logic clk);
  logic [3:0] level;
  logic busy;
  logic wr;
endinterface

package rf_pkg;
  typedef bit [63:0] uvm_reg_data_t;
  typedef class uvm_reg;
  class uvm_reg_field;
    uvm_reg_data_t m;
    function void configure(uvm_reg parent, int unsigned size, int unsigned lsb_pos, string access,
                            bit volatile = 0, uvm_reg_data_t reset = 0, bit has_reset = 1);
    endfunction
    function uvm_reg_data_t get_mirrored_value(); return m; endfunction
  endclass
  class uvm_reg;
    function void add_hdl_path_slice(string name, int offset, int size, bit first = 0, string kind = "RTL");
    endfunction
  endclass
  class uvm_reg_block;
    function void set_hdl_path_root(string path, string kind = "RTL"); endfunction
  endclass

  class rf_reg_cfg extends uvm_reg;
    rand uvm_reg_field mode;
    virtual function void build();
      mode = new();
      mode.configure(this, 2, 0, "RW");
    endfunction
  endclass
  class rf_reg_block extends uvm_reg_block;
    rand rf_reg_cfg cfg;
    virtual function void build();
      cfg = new();
      cfg.build();
      cfg.add_hdl_path_slice("cfg_q", 0, 2, 0, "RTL");
      set_hdl_path_root("rf_tb.dut", "RTL");
    endfunction
  endclass

  class rf_cov;
    covergroup lvl_cg with function sample(int lvl);
      cp_lvl : coverpoint lvl { bins l[] = {[0:8]}; bins never = {12}; }
    endgroup
    covergroup mode_cg with function sample(bit [1:0] m);
      cp_mode : coverpoint m;
    endgroup
    covergroup busy_cg with function sample(bit b);
      cp_busy : coverpoint b;
    endgroup
    covergroup wr_cg with function sample(bit w);
      cp_wr : coverpoint w;
    endgroup
    covergroup tb_cg with function sample(int x);
      cp_x : coverpoint x { bins one = {1}; }
    endgroup
    function new(); lvl_cg = new(); mode_cg = new(); busy_cg = new(); wr_cg = new(); tb_cg = new(); endfunction
  endclass

  class rf_scoreboard;
    virtual rf_if vif;
    rf_reg_block ral;
    rf_cov cov;
    int model_x;
    task run();
      forever begin
        @(posedge vif.clk);
        cov.lvl_cg.sample(vif.level);
        cov.mode_cg.sample(ral.cfg.mode.get_mirrored_value());
        cov.busy_cg.sample(vif.busy);
        cov.wr_cg.sample(vif.wr);
        cov.tb_cg.sample(model_x);
      end
    endtask
  endclass
endpackage

module rf_tb;
  logic clk = 0, rst_n = 0, wr, rd, cfg_we;
  logic [1:0] cfg_wd;
  logic [3:0] lvl;
  rf_if rif (clk);
  rf_dut dut (.clk_i(clk), .rst_ni(rst_n), .wr_i(wr), .rd_i(rd), .cfg_we_i(cfg_we), .cfg_wd_i(cfg_wd),
              .level_o(lvl));
  assign rif.level = lvl;
  assign rif.busy = dut.busy;
  assign rif.wr = wr;
  initial begin
    rf_pkg::rf_scoreboard sb = new();
    sb.vif = rif;
  end
endmodule
