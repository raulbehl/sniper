// LRM conformance, clause 19 (IEEE 1800-2023): the standard's worked examples, as written there.
// test_lrm.py drives fvgen's module of each covergroup with every value (value bins), the LRM's sample
// traces (transition bins) and value pairs (cross bins), and compares the bins hit with what the text
// of 1800-2023 states for the example. Section numbers are 1800-2023.
module l19;
  logic clk;

  // 19.5.1: single bin of values and a range, open arrays (overlapping values), $ bound, default array
  bit [9:0] v_a;
  covergroup g_bins;
    coverpoint v_a {
      bins a = { [0:63], 65 };
      bins b[] = { [127:150], [148:191] };
      bins c[] = { 200, 201, 202 };
      bins d = { [1000:$] };
      bins others[] = default;
    }
  endgroup

  // 19.5.1: fixed number of bins: <1,2,3>, <4,5,6>, <7,8,9>, <10,1,4,7>; and <1>, <4>, <7>, < >, < >
  bit [3:0] f;
  covergroup g_fixed;
    cf: coverpoint f { bins fixed[4] = { [1:10], 1, 4, 7 }; bins few[5] = { 1, 4, 7 }; }
  endgroup

  // 19.5.3: automatic bins, M = 3 and N = 3: <0,1>, <2,3>, <4,5,6,7>
  bit [2:0] w;
  covergroup g_auto;
    cw: coverpoint w { option.auto_bin_max = 3; }
  endgroup

  // 19.5.1.1: with clause on a range list and on the coverpoint name
  bit [7:0] x;
  covergroup g_with;
    cx: coverpoint x { bins mod3[] = { [0:255] } with (item % 3 == 0); bins top[] = cx with (item > 8'd250); }
  endgroup

  // 19.5.2: transition bins; sampled values 4 5 7 11 8 12 2 2 3: allother increments on 7 and on 8
  bit [4:1] t;
  covergroup g_trans;
    ct: coverpoint t {
      bins sa = (4 => 5 => 6), ([7:9], 10 => 11, 12);
      bins sb[] = (4 => 5 => 6), ([7:9], 10 => 11, 12);
      bins sc = (12 => 3 [-> 1]);
      bins allother = default sequence;
    }
  endgroup

  // 19.5.2: repetitions; sampled values 1 4 3 2 3 3 2 2 3 2 3 1 5 5 5 5 5 5:
  // b2 on samples 8, 10; b3 on 6, 9, 11; b5 on 15..18; b6 on 12; b7 on 13
  bit [2:0] v;
  covergroup g_rep;
    cv: coverpoint v {
      bins b2 = (2 [-> 3:5]);
      bins b3 = (3 [-> 3:5]);
      bins b5 = (5 [* 3]);
      bins b6 = (1 => 3 [-> 4:6] => 1);
      bins b7 = (1 => 2 [= 3:6] => 5);
    }
  endgroup

  // 19.5.4: wildcard value bins (12..15; an array of 4) and a wildcard transition (0,1 => 2,3)
  bit [3:0] wv;
  bit [1:0] wt;
  covergroup g_wild;
    cwv: coverpoint wv { wildcard bins g12_15 = { 4'b11?? }; wildcard bins g12_15_array[] = { 4'b11?? }; }
    cwt: coverpoint wt { wildcard bins T0_3 = (2'b0x => 2'b1x); wildcard bins T0_3_array[] = (2'b0x => 2'b1x); }
  endgroup

  // 19.5.5 / 19.5.6: ignored values leave the automatic bins; illegal values
  bit [3:0] ia, ib;
  covergroup g_excl;
    cia: coverpoint ia { ignore_bins ignore_vals = {7, 8}; ignore_bins ignore_trans = (1 => 3 => 5); }
    cib: coverpoint ib { bins all = {[0:15]}; illegal_bins bad_vals = {1, 2, 3}; illegal_bins bad_trans = (4 => 5 => 6); }
  endgroup

  // 19.5.7: value resolution: b1 = {1:7}, b2 = {1:7}, b3 = {1, 2, 3}, b4 = {-1, 1, 2, 3}
  bit [2:0] p1;
  bit signed [2:0] p2;
  covergroup g_res;
    cp1: coverpoint p1 { bins b1 = { 1, [2:5], [6:10] }; bins b2 = { -1, [1:10], 15 }; }
    cp2: coverpoint p2 { bins b3 = { 1, [2:5], [6:10] }; bins b4 = { -1, [1:10], 15 }; }
  endgroup

  // 19.6: cross of a variable (implicit coverpoint, 16 automatic bins) and a coverpoint of 10 bins
  bit [31:0] a_var;
  bit [3:0] b_var;
  covergroup g_cov3;
    A: coverpoint a_var { bins yy[] = { [0:9] }; }
    CC: cross b_var, A;
  endgroup

  // 19.6.1: a user cross bin and the automatic bins it does not overlap
  int i, j;
  covergroup g_ct;
    coverpoint i { bins i[] = { [0:1] }; }
    coverpoint j { bins j[] = { [0:1] }; }
    x1: cross i, j;
    x2: cross i, j { bins i_zero = binsof(i) intersect { 0 }; }
  endgroup

  // 19.6.1.1: c1 = <a1,b1..b4>; c2 = <a2,b*>, <a1,b2>, <a3,b2>, <a4,b2>; c3 = <a1,b4>;
  // retained automatic: <a3,b1>, <a4,b1>, <a3,b3>, <a4,b3>, <a3,b4>, <a4,b4>
  bit [7:0] v_c, v_d;
  covergroup g_sel;
    a: coverpoint v_c { bins a1 = { [0:63] }; bins a2 = { [64:127] }; bins a3 = { [128:191] }; bins a4 = { [192:255] }; }
    b: coverpoint v_d { bins b1 = {0}; bins b2 = { [1:84] }; bins b3 = { [85:169] }; bins b4 = { [170:255] }; }
    c: cross a, b {
      bins c1 = ! binsof(a) intersect {[100:200]};
      bins c2 = binsof(a.a2) || binsof(b.b2);
      bins c3 = binsof(a.a1) && binsof(b.b4);
    }
  endgroup

  // 19.6.2 / 19.6.3: excluded and illegal cross products (illegal wins over ignore)
  bit [2:0] ya, yb;
  covergroup g_yy;
    cya: coverpoint ya;
    cyb: coverpoint yb;
    yx: cross cya, cyb {
      ignore_bins ignore = binsof(cya) intersect { 5, [1:3] };
      illegal_bins illegal = binsof(cyb) intersect { 6 };
    }
  endgroup

  // 19.5.1 per-bin iff; coverpoint iff; cross iff
  bit [1:0] q;
  bit en_b, en_cp;
  covergroup g_iff;
    cq: coverpoint q iff (en_cp) { bins lo = {[0:1]} iff (en_b); bins hi = {[2:3]}; }
  endgroup

  g_bins gi_bins = new; g_fixed gi_fixed = new; g_auto gi_auto = new; g_with gi_with = new;
  g_trans gi_trans = new; g_rep gi_rep = new; g_wild gi_wild = new; g_excl gi_excl = new; g_res gi_res = new;
  g_cov3 gi_cov3 = new; g_ct gi_ct = new; g_sel gi_sel = new; g_yy gi_yy = new; g_iff gi_iff = new;
  initial begin
    gi_bins.sample(); gi_fixed.sample(); gi_auto.sample(); gi_with.sample(); gi_trans.sample(); gi_rep.sample();
    gi_wild.sample(); gi_excl.sample(); gi_res.sample(); gi_cov3.sample(); gi_ct.sample(); gi_sel.sample();
    gi_yy.sample(); gi_iff.sample();
  end
endmodule
