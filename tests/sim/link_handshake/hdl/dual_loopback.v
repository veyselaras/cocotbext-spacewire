`timescale 1ns/1ps

// Two SpaceWire endpoints cross-connected on the same die: A's outputs feed
// B's inputs and vice versa.  Each side keeps its own {spw_a_*, spw_b_*}
// signal group so a ``SpaceWireBus(entity, prefix="spw_a")`` and
// ``SpaceWireBus(entity, prefix="spw_b")`` resolve independently.
module dual_loopback (
    input  wire spw_a_do,
    input  wire spw_a_so,
    output wire spw_a_di,
    output wire spw_a_si,

    input  wire spw_b_do,
    input  wire spw_b_so,
    output wire spw_b_di,
    output wire spw_b_si
);
    assign spw_a_di = (spw_b_do === 1'b1) ? 1'b1 : 1'b0;
    assign spw_a_si = (spw_b_so === 1'b1) ? 1'b1 : 1'b0;
    assign spw_b_di = (spw_a_do === 1'b1) ? 1'b1 : 1'b0;
    assign spw_b_si = (spw_a_so === 1'b1) ? 1'b1 : 1'b0;
endmodule
