`timescale 1ns/1ps

module loopback_top (
    input  wire spw_do,
    input  wire spw_so,
    output wire spw_di,
    output wire spw_si
);
    assign spw_di = (spw_do === 1'b1) ? 1'b1 : 1'b0;
    assign spw_si = (spw_so === 1'b1) ? 1'b1 : 1'b0;
endmodule