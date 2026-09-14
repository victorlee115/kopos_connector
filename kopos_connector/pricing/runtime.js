/*! dinero.js 2.0.2
The MIT License (MIT)

Copyright (c) 2018-present Sarah Dayan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
/*! canonicalize 2.1.0
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
*/
/*! edmonds-blossom-fixed 1.0.1
The MIT License (MIT)

Copyright (c) 2015 mattkrick

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

*/
var JiJiPromotionRuntime = (() => {
  var __create = Object.create;
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __getProtoOf = Object.getPrototypeOf;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
  var __commonJS = (cb, mod) => function __require() {
    try {
      return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
    } catch (e) {
      throw mod = 0, e;
    }
  };
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
    // If the importer is in node compatibility mode or this is not an ESM
    // file that has been converted to a CommonJS file using a Babel-
    // compatible transform (i.e. "__esModule" has not been set), then set
    // "default" to the CommonJS "module.exports" for node compatibility.
    isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
    mod
  ));
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);
  var __publicField = (obj, key, value) => __defNormalProp(obj, typeof key !== "symbol" ? key + "" : key, value);

  // node_modules/edmonds-blossom-fixed/app/blossom.js
  var require_blossom = __commonJS({
    "node_modules/edmonds-blossom-fixed/app/blossom.js"(exports, module) {
      module.exports = function(edges, maxCardinality) {
        if (edges.length === 0) {
          return edges;
        }
        var edmonds = new Edmonds(edges, maxCardinality);
        return edmonds.maxWeightMatching();
      };
      var Edmonds = function(edges, maxCardinality) {
        this.edges = edges;
        this.maxCardinality = maxCardinality;
        this.nEdge = edges.length;
        this.init();
      };
      Edmonds.prototype.maxWeightMatching = function() {
        for (var t = 0; t < this.nVertex; t++) {
          this.label = filledArray(2 * this.nVertex, 0);
          this.bestEdge = filledArray(2 * this.nVertex, -1);
          this.blossomBestEdges = initArrArr(2 * this.nVertex);
          this.allowEdge = filledArray(this.nEdge, false);
          this.queue = [];
          for (var v = 0; v < this.nVertex; v++) {
            if (this.mate[v] === -1 && this.label[this.inBlossom[v]] === 0) {
              this.assignLabel(v, 1, -1);
            }
          }
          var augmented = false;
          while (true) {
            while (this.queue.length > 0 && !augmented) {
              v = this.queue.pop();
              for (var ii = 0; ii < this.neighbend[v].length; ii++) {
                var p = this.neighbend[v][ii];
                var k = ~~(p / 2);
                var w = this.endpoint[p];
                if (this.inBlossom[v] === this.inBlossom[w]) continue;
                if (!this.allowEdge[k]) {
                  var kSlack = this.slack(k);
                  if (kSlack <= 0) {
                    this.allowEdge[k] = true;
                  }
                }
                if (this.allowEdge[k]) {
                  if (this.label[this.inBlossom[w]] === 0) {
                    this.assignLabel(w, 2, p ^ 1);
                  } else if (this.label[this.inBlossom[w]] === 1) {
                    var base = this.scanBlossom(v, w);
                    if (base >= 0) {
                      this.addBlossom(base, k);
                    } else {
                      this.augmentMatching(k);
                      augmented = true;
                      break;
                    }
                  } else if (this.label[w] === 0) {
                    this.label[w] = 2;
                    this.labelEnd[w] = p ^ 1;
                  }
                } else if (this.label[this.inBlossom[w]] === 1) {
                  var b = this.inBlossom[v];
                  if (this.bestEdge[b] === -1 || kSlack < this.slack(this.bestEdge[b])) {
                    this.bestEdge[b] = k;
                  }
                } else if (this.label[w] === 0) {
                  if (this.bestEdge[w] === -1 || kSlack < this.slack(this.bestEdge[w])) {
                    this.bestEdge[w] = k;
                  }
                }
              }
            }
            if (augmented) break;
            var deltaType = -1;
            var delta = [];
            var deltaEdge = [];
            var deltaBlossom = [];
            if (!this.maxCardinality) {
              deltaType = 1;
              delta = getMin(this.dualVar, 0, this.nVertex - 1);
            }
            for (v = 0; v < this.nVertex; v++) {
              if (this.label[this.inBlossom[v]] === 0 && this.bestEdge[v] !== -1) {
                var d = this.slack(this.bestEdge[v]);
                if (deltaType === -1 || d < delta) {
                  delta = d;
                  deltaType = 2;
                  deltaEdge = this.bestEdge[v];
                }
              }
            }
            for (b = 0; b < 2 * this.nVertex; b++) {
              if (this.blossomParent[b] === -1 && this.label[b] === 1 && this.bestEdge[b] !== -1) {
                kSlack = this.slack(this.bestEdge[b]);
                d = kSlack / 2;
                if (deltaType === -1 || d < delta) {
                  delta = d;
                  deltaType = 3;
                  deltaEdge = this.bestEdge[b];
                }
              }
            }
            for (b = this.nVertex; b < this.nVertex * 2; b++) {
              if (this.blossomBase[b] >= 0 && this.blossomParent[b] === -1 && this.label[b] === 2 && (deltaType === -1 || this.dualVar[b] < delta)) {
                delta = this.dualVar[b];
                deltaType = 4;
                deltaBlossom = b;
              }
            }
            if (deltaType === -1) {
              deltaType = 1;
              delta = Math.max(0, getMin(this.dualVar, 0, this.nVertex - 1));
            }
            for (v = 0; v < this.nVertex; v++) {
              var curLabel = this.label[this.inBlossom[v]];
              if (curLabel === 1) {
                this.dualVar[v] -= delta;
              } else if (curLabel === 2) {
                this.dualVar[v] += delta;
              }
            }
            for (b = this.nVertex; b < this.nVertex * 2; b++) {
              if (this.blossomBase[b] >= 0 && this.blossomParent[b] === -1) {
                if (this.label[b] === 1) {
                  this.dualVar[b] += delta;
                } else if (this.label[b] === 2) {
                  this.dualVar[b] -= delta;
                }
              }
            }
            if (deltaType === 1) {
              break;
            } else if (deltaType === 2) {
              this.allowEdge[deltaEdge] = true;
              var i = this.edges[deltaEdge][0];
              var j = this.edges[deltaEdge][1];
              var wt = this.edges[deltaEdge][2];
              if (this.label[this.inBlossom[i]] === 0) {
                i = i ^ j;
                j = j ^ i;
                i = i ^ j;
              }
              this.queue.push(i);
            } else if (deltaType === 3) {
              this.allowEdge[deltaEdge] = true;
              i = this.edges[deltaEdge][0];
              j = this.edges[deltaEdge][1];
              wt = this.edges[deltaEdge][2];
              this.queue.push(i);
            } else if (deltaType === 4) {
              this.expandBlossom(deltaBlossom, false);
            }
          }
          if (!augmented) break;
          for (b = this.nVertex; b < this.nVertex * 2; b++) {
            if (this.blossomParent[b] === -1 && this.blossomBase[b] >= 0 && this.label[b] === 1 && this.dualVar[b] === 0) {
              this.expandBlossom(b, true);
            }
          }
        }
        for (v = 0; v < this.nVertex; v++) {
          if (this.mate[v] >= 0) {
            this.mate[v] = this.endpoint[this.mate[v]];
          }
        }
        for (v = 0; v < this.nVertex; v++) {
        }
        return this.mate;
      };
      Edmonds.prototype.slack = function(k) {
        var i = this.edges[k][0];
        var j = this.edges[k][1];
        var wt = this.edges[k][2];
        return this.dualVar[i] + this.dualVar[j] - 2 * wt;
      };
      Edmonds.prototype.blossomLeaves = function(b) {
        if (b < this.nVertex) {
          return [b];
        }
        var leaves = [];
        var childList = this.blossomChilds[b];
        for (var t = 0; t < childList.length; t++) {
          if (childList[t] <= this.nVertex) {
            leaves.push(childList[t]);
          } else {
            var leafList = this.blossomLeaves(childList[t]);
            for (var v = 0; v < leafList.length; v++) {
              leaves.push(leafList[v]);
            }
          }
        }
        return leaves;
      };
      Edmonds.prototype.assignLabel = function(w, t, p) {
        var b = this.inBlossom[w];
        this.label[w] = this.label[b] = t;
        this.labelEnd[w] = this.labelEnd[b] = p;
        this.bestEdge[w] = this.bestEdge[b] = -1;
        if (t === 1) {
          this.queue.push.apply(this.queue, this.blossomLeaves(b));
        } else if (t === 2) {
          var base = this.blossomBase[b];
          this.assignLabel(this.endpoint[this.mate[base]], 1, this.mate[base] ^ 1);
        }
      };
      Edmonds.prototype.scanBlossom = function(v, w) {
        var path = [];
        var base = -1;
        while (v !== -1 || w !== -1) {
          var b = this.inBlossom[v];
          if (this.label[b] & 4) {
            base = this.blossomBase[b];
            break;
          }
          path.push(b);
          this.label[b] = 5;
          if (this.labelEnd[b] === -1) {
            v = -1;
          } else {
            v = this.endpoint[this.labelEnd[b]];
            b = this.inBlossom[v];
            v = this.endpoint[this.labelEnd[b]];
          }
          if (w !== -1) {
            v = v ^ w;
            w = w ^ v;
            v = v ^ w;
          }
        }
        for (var ii = 0; ii < path.length; ii++) {
          b = path[ii];
          this.label[b] = 1;
        }
        return base;
      };
      Edmonds.prototype.addBlossom = function(base, k) {
        var v = this.edges[k][0];
        var w = this.edges[k][1];
        var wt = this.edges[k][2];
        var bb = this.inBlossom[base];
        var bv = this.inBlossom[v];
        var bw = this.inBlossom[w];
        var b = this.unusedBlossoms.pop();
        this.blossomBase[b] = base;
        this.blossomParent[b] = -1;
        this.blossomParent[bb] = b;
        var path = this.blossomChilds[b] = [];
        var endPs = this.blossomEndPs[b] = [];
        while (bv !== bb) {
          this.blossomParent[bv] = b;
          path.push(bv);
          endPs.push(this.labelEnd[bv]);
          v = this.endpoint[this.labelEnd[bv]];
          bv = this.inBlossom[v];
        }
        path.push(bb);
        path.reverse();
        endPs.reverse();
        endPs.push(2 * k);
        while (bw !== bb) {
          this.blossomParent[bw] = b;
          path.push(bw);
          endPs.push(this.labelEnd[bw] ^ 1);
          w = this.endpoint[this.labelEnd[bw]];
          bw = this.inBlossom[w];
        }
        this.label[b] = 1;
        this.labelEnd[b] = this.labelEnd[bb];
        this.dualVar[b] = 0;
        var leaves = this.blossomLeaves(b);
        for (var ii = 0; ii < leaves.length; ii++) {
          v = leaves[ii];
          if (this.label[this.inBlossom[v]] === 2) {
            this.queue.push(v);
          }
          this.inBlossom[v] = b;
        }
        var bestEdgeTo = filledArray(2 * this.nVertex, -1);
        for (ii = 0; ii < path.length; ii++) {
          bv = path[ii];
          if (this.blossomBestEdges[bv].length === 0) {
            var nbLists = [];
            leaves = this.blossomLeaves(bv);
            for (var x = 0; x < leaves.length; x++) {
              v = leaves[x];
              nbLists[x] = [];
              for (var y = 0; y < this.neighbend[v].length; y++) {
                var p = this.neighbend[v][y];
                nbLists[x].push(~~(p / 2));
              }
            }
          } else {
            nbLists = [this.blossomBestEdges[bv]];
          }
          for (x = 0; x < nbLists.length; x++) {
            var nbList = nbLists[x];
            for (y = 0; y < nbList.length; y++) {
              k = nbList[y];
              var i = this.edges[k][0];
              var j = this.edges[k][1];
              wt = this.edges[k][2];
              if (this.inBlossom[j] === b) {
                i = i ^ j;
                j = j ^ i;
                i = i ^ j;
              }
              var bj = this.inBlossom[j];
              if (bj !== b && this.label[bj] === 1 && (bestEdgeTo[bj] === -1 || this.slack(k) < this.slack(bestEdgeTo[bj]))) {
                bestEdgeTo[bj] = k;
              }
            }
          }
          this.blossomBestEdges[bv] = [];
          this.bestEdge[bv] = -1;
        }
        var be = [];
        for (ii = 0; ii < bestEdgeTo.length; ii++) {
          k = bestEdgeTo[ii];
          if (k !== -1) {
            be.push(k);
          }
        }
        this.blossomBestEdges[b] = be;
        this.bestEdge[b] = -1;
        for (ii = 0; ii < this.blossomBestEdges[b].length; ii++) {
          k = this.blossomBestEdges[b][ii];
          if (this.bestEdge[b] === -1 || this.slack(k) < this.slack(this.bestEdge[b])) {
            this.bestEdge[b] = k;
          }
        }
      };
      Edmonds.prototype.expandBlossom = function(b, endStage) {
        for (var ii = 0; ii < this.blossomChilds[b].length; ii++) {
          var s = this.blossomChilds[b][ii];
          this.blossomParent[s] = -1;
          if (s < this.nVertex) {
            this.inBlossom[s] = s;
          } else if (endStage && this.dualVar[s] === 0) {
            this.expandBlossom(s, endStage);
          } else {
            var leaves = this.blossomLeaves(s);
            for (var jj = 0; jj < leaves.length; jj++) {
              var v = leaves[jj];
              this.inBlossom[v] = s;
            }
          }
        }
        if (!endStage && this.label[b] === 2) {
          var entryChild = this.inBlossom[this.endpoint[this.labelEnd[b] ^ 1]];
          var j = this.blossomChilds[b].indexOf(entryChild);
          if (j & 1) {
            j -= this.blossomChilds[b].length;
            var jStep = 1;
            var endpTrick = 0;
          } else {
            jStep = -1;
            endpTrick = 1;
          }
          var p = this.labelEnd[b];
          while (j !== 0) {
            this.label[this.endpoint[p ^ 1]] = 0;
            this.label[this.endpoint[pIndex(this.blossomEndPs[b], j - endpTrick) ^ endpTrick ^ 1]] = 0;
            this.assignLabel(this.endpoint[p ^ 1], 2, p);
            this.allowEdge[~~(pIndex(this.blossomEndPs[b], j - endpTrick) / 2)] = true;
            j += jStep;
            p = pIndex(this.blossomEndPs[b], j - endpTrick) ^ endpTrick;
            this.allowEdge[~~(p / 2)] = true;
            j += jStep;
          }
          var bv = pIndex(this.blossomChilds[b], j);
          this.label[this.endpoint[p ^ 1]] = this.label[bv] = 2;
          this.labelEnd[this.endpoint[p ^ 1]] = this.labelEnd[bv] = p;
          this.bestEdge[bv] = -1;
          j += jStep;
          while (pIndex(this.blossomChilds[b], j) !== entryChild) {
            bv = pIndex(this.blossomChilds[b], j);
            if (this.label[bv] === 1) {
              j += jStep;
              continue;
            }
            leaves = this.blossomLeaves(bv);
            for (ii = 0; ii < leaves.length; ii++) {
              v = leaves[ii];
              if (this.label[v] !== 0) break;
            }
            if (this.label[v] !== 0) {
              this.label[v] = 0;
              this.label[this.endpoint[this.mate[this.blossomBase[bv]]]] = 0;
              this.assignLabel(v, 2, this.labelEnd[v]);
            }
            j += jStep;
          }
        }
        this.label[b] = this.labelEnd[b] = -1;
        this.blossomEndPs[b] = this.blossomChilds[b] = [];
        this.blossomBase[b] = -1;
        this.blossomBestEdges[b] = [];
        this.bestEdge[b] = -1;
        this.unusedBlossoms.push(b);
      };
      Edmonds.prototype.augmentBlossom = function(b, v) {
        var i, j;
        var t = v;
        while (this.blossomParent[t] !== b) {
          t = this.blossomParent[t];
        }
        if (t > this.nVertex) {
          this.augmentBlossom(t, v);
        }
        i = j = this.blossomChilds[b].indexOf(t);
        if (i & 1) {
          j -= this.blossomChilds[b].length;
          var jStep = 1;
          var endpTrick = 0;
        } else {
          jStep = -1;
          endpTrick = 1;
        }
        while (j !== 0) {
          j += jStep;
          t = pIndex(this.blossomChilds[b], j);
          var p = pIndex(this.blossomEndPs[b], j - endpTrick) ^ endpTrick;
          if (t >= this.nVertex) {
            this.augmentBlossom(t, this.endpoint[p]);
          }
          j += jStep;
          t = pIndex(this.blossomChilds[b], j);
          if (t >= this.nVertex) {
            this.augmentBlossom(t, this.endpoint[p ^ 1]);
          }
          this.mate[this.endpoint[p]] = p ^ 1;
          this.mate[this.endpoint[p ^ 1]] = p;
        }
        this.blossomChilds[b] = this.blossomChilds[b].slice(i).concat(this.blossomChilds[b].slice(0, i));
        this.blossomEndPs[b] = this.blossomEndPs[b].slice(i).concat(this.blossomEndPs[b].slice(0, i));
        this.blossomBase[b] = this.blossomBase[this.blossomChilds[b][0]];
      };
      Edmonds.prototype.augmentMatching = function(k) {
        var v = this.edges[k][0];
        var w = this.edges[k][1];
        for (var ii = 0; ii < 2; ii++) {
          if (ii === 0) {
            var s = v;
            var p = 2 * k + 1;
          } else {
            s = w;
            p = 2 * k;
          }
          while (true) {
            var bs = this.inBlossom[s];
            if (bs >= this.nVertex) {
              this.augmentBlossom(bs, s);
            }
            this.mate[s] = p;
            if (this.labelEnd[bs] === -1) break;
            var t = this.endpoint[this.labelEnd[bs]];
            var bt = this.inBlossom[t];
            s = this.endpoint[this.labelEnd[bt]];
            var j = this.endpoint[this.labelEnd[bt] ^ 1];
            if (bt >= this.nVertex) {
              this.augmentBlossom(bt, j);
            }
            this.mate[j] = this.labelEnd[bt];
            p = this.labelEnd[bt] ^ 1;
          }
        }
      };
      Edmonds.prototype.init = function() {
        this.nVertexInit();
        this.maxWeightInit();
        this.endpointInit();
        this.neighbendInit();
        this.mate = filledArray(this.nVertex, -1);
        this.label = filledArray(2 * this.nVertex, 0);
        this.labelEnd = filledArray(2 * this.nVertex, -1);
        this.inBlossomInit();
        this.blossomParent = filledArray(2 * this.nVertex, -1);
        this.blossomChilds = initArrArr(2 * this.nVertex);
        this.blossomBaseInit();
        this.blossomEndPs = initArrArr(2 * this.nVertex);
        this.bestEdge = filledArray(2 * this.nVertex, -1);
        this.blossomBestEdges = initArrArr(2 * this.nVertex);
        this.unusedBlossomsInit();
        this.dualVarInit();
        this.allowEdge = filledArray(this.nEdge, false);
        this.queue = [];
      };
      Edmonds.prototype.blossomBaseInit = function() {
        var base = [];
        for (var i = 0; i < this.nVertex; i++) {
          base[i] = i;
        }
        var negs = filledArray(this.nVertex, -1);
        this.blossomBase = base.concat(negs);
      };
      Edmonds.prototype.dualVarInit = function() {
        var mw = filledArray(this.nVertex, this.maxWeight);
        var zeros = filledArray(this.nVertex, 0);
        this.dualVar = mw.concat(zeros);
      };
      Edmonds.prototype.unusedBlossomsInit = function() {
        var i, unusedBlossoms = [];
        for (i = this.nVertex; i < 2 * this.nVertex; i++) {
          unusedBlossoms.push(i);
        }
        this.unusedBlossoms = unusedBlossoms;
      };
      Edmonds.prototype.inBlossomInit = function() {
        var i, inBlossom = [];
        for (i = 0; i < this.nVertex; i++) {
          inBlossom[i] = i;
        }
        this.inBlossom = inBlossom;
      };
      Edmonds.prototype.neighbendInit = function() {
        var k, i, j;
        var neighbend = initArrArr(this.nVertex);
        for (k = 0; k < this.nEdge; k++) {
          i = this.edges[k][0];
          j = this.edges[k][1];
          neighbend[i].push(2 * k + 1);
          neighbend[j].push(2 * k);
        }
        this.neighbend = neighbend;
      };
      Edmonds.prototype.endpointInit = function() {
        var p;
        var endpoint = [];
        for (p = 0; p < 2 * this.nEdge; p++) {
          endpoint[p] = this.edges[~~(p / 2)][p % 2];
        }
        this.endpoint = endpoint;
      };
      Edmonds.prototype.nVertexInit = function() {
        var nVertex = 0;
        for (var k = 0; k < this.nEdge; k++) {
          var i = this.edges[k][0];
          var j = this.edges[k][1];
          if (i >= nVertex) nVertex = i + 1;
          if (j >= nVertex) nVertex = j + 1;
        }
        this.nVertex = nVertex;
      };
      Edmonds.prototype.maxWeightInit = function() {
        var maxWeight = 0;
        for (var k = 0; k < this.nEdge; k++) {
          var weight = this.edges[k][2];
          if (weight > maxWeight) {
            maxWeight = weight;
          }
        }
        this.maxWeight = maxWeight;
      };
      function filledArray(len, fill) {
        var i, newArray = [];
        for (i = 0; i < len; i++) {
          newArray[i] = fill;
        }
        return newArray;
      }
      function initArrArr(len) {
        var arr = [];
        for (var i = 0; i < len; i++) {
          arr[i] = [];
        }
        return arr;
      }
      function getMin(arr, start, end) {
        var min = Infinity;
        for (var i = start; i <= end; i++) {
          if (arr[i] < min) {
            min = arr[i];
          }
        }
        return min;
      }
      function pIndex(arr, idx) {
        return idx < 0 ? arr[arr.length + idx] : arr[idx];
      }
    }
  });

  // node_modules/canonicalize/lib/canonicalize.js
  var require_canonicalize = __commonJS({
    "node_modules/canonicalize/lib/canonicalize.js"(exports, module) {
      "use strict";
      module.exports = function serialize(object) {
        if (typeof object === "number" && isNaN(object)) {
          throw new Error("NaN is not allowed");
        }
        if (typeof object === "number" && !isFinite(object)) {
          throw new Error("Infinity is not allowed");
        }
        if (object === null || typeof object !== "object") {
          return JSON.stringify(object);
        }
        if (object.toJSON instanceof Function) {
          return serialize(object.toJSON());
        }
        if (Array.isArray(object)) {
          const values2 = object.reduce((t, cv, ci) => {
            const comma = ci === 0 ? "" : ",";
            const value = cv === void 0 || typeof cv === "symbol" ? null : cv;
            return `${t}${comma}${serialize(value)}`;
          }, "");
          return `[${values2}]`;
        }
        const values = Object.keys(object).sort().reduce((t, cv) => {
          if (object[cv] === void 0 || typeof object[cv] === "symbol") {
            return t;
          }
          const comma = t.length === 0 ? "" : ",";
          return `${t}${comma}${serialize(cv)}:${serialize(object[cv])}`;
        }, "");
        return `{${values}}`;
      };
    }
  });

  // promotion-runtime-entry.ts
  var promotion_runtime_entry_exports = {};
  __export(promotion_runtime_entry_exports, {
    canonicalPromotionContent: () => canonicalPromotionContent,
    nextPromotionOccurrences: () => nextPromotionOccurrences,
    promotionScheduleContains: () => promotionScheduleContains,
    quotePromotionBasketTotals: () => quotePromotionBasketTotals,
    quotePromotions: () => quotePromotions,
    readPromotionSchedule: () => readPromotionSchedule
  });

  // src/services/promotions/kernel.ts
  var import_edmonds_blossom_fixed = __toESM(require_blossom());

  // src/services/sync/generated/promotion-contracts.ts
  var PROMOTION_FIELD_ENUMS = {
    "promotion_type": [
      "item_discount",
      "order_discount",
      "happy_hour",
      "buy_x_get_y",
      "nth_item_discount"
    ],
    "activation_mode": [
      "automatic",
      "manual_selectable"
    ],
    "stacking_policy": [
      "exclusive",
      "stackable",
      "best_price"
    ],
    "discount_target": [
      "cheaper_eligible"
    ],
    "discount_type": [
      "percentage",
      "fixed_amount",
      "fixed_price",
      "free_item"
    ],
    "repeat_mode": [
      "once",
      "repeat"
    ],
    "eligible_scope_mode": [
      "same_item",
      "eligible_pool"
    ],
    "comparison_basis": [
      "base_item_only"
    ],
    "discount_basis": [
      "base_item_only"
    ],
    "modifier_policy": [
      "excluded_by_default",
      "include_paid_modifiers"
    ]
  };

  // src/services/promotions/time-guard.ts
  var PROMOTION_TIME_ROLLBACK_TOLERANCE_MS = 5 * 60 * 1e3;
  var PROMOTION_TIME_DEFAULT_PERSISTENCE_INTERVAL_MS = 60 * 1e3;

  // node_modules/dinero.js/dist/esm/trimScale-C3-DjbPM.js
  var INVALID_RATIOS_MESSAGE = "Ratios are invalid.";
  function assert(condition, message) {
    if (!condition) throw new Error(`[Dinero.js] ${message}`);
  }
  function createDinero({ calculator: calculator2, onCreate, formatter = {
    toNumber: Number,
    toString: String
  } }) {
    return function dinero2({ amount, currency: { code, base, exponent }, scale = exponent }) {
      const currency = {
        code,
        base,
        exponent
      };
      onCreate?.({
        amount,
        currency,
        scale
      });
      return {
        calculator: calculator2,
        formatter,
        create: dinero2,
        toJSON() {
          return {
            amount,
            currency,
            scale
          };
        }
      };
    };
  }
  var DineroComparisonOperator = /* @__PURE__ */ (function(DineroComparisonOperator2) {
    DineroComparisonOperator2[DineroComparisonOperator2["LT"] = -1] = "LT";
    DineroComparisonOperator2[DineroComparisonOperator2["EQ"] = 0] = "EQ";
    DineroComparisonOperator2[DineroComparisonOperator2["GT"] = 1] = "GT";
    return DineroComparisonOperator2;
  })({});
  function equal$2(calculator2) {
    return (subject, comparator) => {
      return calculator2.compare(subject, comparator) === DineroComparisonOperator.EQ;
    };
  }
  function lessThan$1(calculator2) {
    return (subject, comparator) => {
      return calculator2.compare(subject, comparator) === DineroComparisonOperator.LT;
    };
  }
  function absolute(calculator2) {
    const equalFn = equal$2(calculator2);
    const lessThanFn = lessThan$1(calculator2);
    const zero2 = calculator2.zero();
    return (input) => {
      if (equalFn(input, zero2)) return zero2;
      if (lessThanFn(input, zero2)) {
        const minusOne = calculator2.decrement(zero2);
        return calculator2.multiply(minusOne, input);
      }
      return input;
    };
  }
  function isArray(maybeArray) {
    return Array.isArray(maybeArray);
  }
  function computeBase(calculator2) {
    return (base) => {
      if (isArray(base)) return base.reduce((acc, curr) => calculator2.multiply(acc, curr));
      return base;
    };
  }
  function greaterThan$1(calculator2) {
    return (subject, comparator) => {
      return calculator2.compare(subject, comparator) === DineroComparisonOperator.GT;
    };
  }
  function greaterThanOrEqual$1(calculator2) {
    return (subject, comparator) => {
      return greaterThan$1(calculator2)(subject, comparator) || equal$2(calculator2)(subject, comparator);
    };
  }
  function distribute(calculator2) {
    return (value, ratios) => {
      const equalFn = equal$2(calculator2);
      const greaterThanFn = greaterThan$1(calculator2);
      const lessThanFn = lessThan$1(calculator2);
      const greaterThanOrEqualFn = greaterThanOrEqual$1(calculator2);
      const zero2 = calculator2.zero();
      const one = calculator2.increment(zero2);
      const total = ratios.reduce((a, b) => calculator2.add(a, b), zero2);
      if (equalFn(total, zero2)) return ratios;
      let remainder = value;
      const shares = ratios.map((ratio) => {
        const share = calculator2.integerDivide(calculator2.multiply(value, ratio), total) || zero2;
        remainder = calculator2.subtract(remainder, share);
        return share;
      });
      const isPositive2 = greaterThanOrEqualFn(value, zero2);
      const compare2 = isPositive2 ? greaterThanFn : lessThanFn;
      const amount = isPositive2 ? one : calculator2.decrement(zero2);
      const sortedIndices = ratios.map((ratio, index) => ({
        ratio,
        index
      })).filter(({ ratio }) => !equalFn(ratio, zero2)).sort((a, b) => greaterThanFn(a.ratio, b.ratio) ? -1 : 1).map(({ index }) => index);
      let i = 0;
      while (compare2(remainder, zero2)) {
        const index = sortedIndices[i % sortedIndices.length];
        shares[index] = calculator2.add(shares[index], amount);
        const newRemainder = calculator2.subtract(remainder, amount);
        if (equalFn(newRemainder, remainder)) break;
        remainder = newRemainder;
        i++;
      }
      return shares;
    };
  }
  function isScaledAmount(amount) {
    return amount?.hasOwnProperty("amount");
  }
  function getAmountAndScale(value, zero2) {
    if (isScaledAmount(value)) return {
      amount: value.amount,
      scale: value?.scale ?? zero2
    };
    return {
      amount: value,
      scale: zero2
    };
  }
  function isHalf(calculator2) {
    const equalFn = equal$2(calculator2);
    const absoluteFn = absolute(calculator2);
    return (input, total) => {
      const remainder = absoluteFn(calculator2.modulo(input, total));
      return equalFn(calculator2.subtract(total, remainder), remainder);
    };
  }
  function maximum$1(calculator2) {
    const lessThanFn = lessThan$1(calculator2);
    return (values) => {
      return values.reduce((acc, curr) => {
        return lessThanFn(acc, curr) ? curr : acc;
      });
    };
  }
  var down = (amount, factor, calculator2) => {
    const greaterThanFn = greaterThan$1(calculator2);
    const equalFn = equal$2(calculator2);
    const zero2 = calculator2.zero();
    const isPositive2 = greaterThanFn(amount, zero2);
    const quotient = calculator2.integerDivide(amount, factor);
    const isInteger = equalFn(calculator2.modulo(amount, factor), zero2);
    if (isPositive2 || isInteger) return quotient;
    return calculator2.decrement(quotient);
  };
  var halfUp = (amount, factor, calculator2) => {
    const greaterThanFn = greaterThan$1(calculator2);
    const isHalfFn = isHalf(calculator2);
    const absoluteFn = absolute(calculator2);
    const zero2 = calculator2.zero();
    const remainder = absoluteFn(calculator2.modulo(amount, factor));
    const isLessThanHalf = greaterThanFn(calculator2.subtract(factor, remainder), remainder);
    const isPositive2 = greaterThanFn(amount, zero2);
    if (isHalfFn(amount, factor) || isPositive2 && !isLessThanHalf || !isPositive2 && isLessThanHalf) return up(amount, factor, calculator2);
    return down(amount, factor, calculator2);
  };
  var up = (amount, factor, calculator2) => {
    const greaterThanFn = greaterThan$1(calculator2);
    const equalFn = equal$2(calculator2);
    const zero2 = calculator2.zero();
    const isPositive2 = greaterThanFn(amount, zero2);
    const quotient = calculator2.integerDivide(amount, factor);
    if (!equalFn(calculator2.modulo(amount, factor), zero2) && isPositive2) return calculator2.increment(quotient);
    return quotient;
  };
  function transformScale$1(calculator2) {
    const greaterThanFn = greaterThan$1(calculator2);
    const computeBaseFn = computeBase(calculator2);
    return function transformScaleFn(...[dineroObject, newScale, divide = down]) {
      const { amount, currency, scale } = dineroObject.toJSON();
      const isLarger = greaterThanFn(newScale, scale);
      const operation = isLarger ? calculator2.multiply : divide;
      const [a, b] = isLarger ? [newScale, scale] : [scale, newScale];
      const base = computeBaseFn(currency.base);
      const factor = calculator2.power(base, calculator2.subtract(a, b));
      return dineroObject.create({
        amount: operation(amount, factor, calculator2),
        currency,
        scale: newScale
      });
    };
  }
  function unsafeAllocate(calculator2) {
    return function allocate2(...[dineroObject, ratios]) {
      const { amount, currency, scale } = dineroObject.toJSON();
      return distribute(calculator2)(amount, ratios.map((ratio) => ratio.amount)).map((share) => {
        return dineroObject.create({
          amount: share,
          currency,
          scale
        });
      });
    };
  }
  function safeAllocate(calculator2) {
    const allocateFn = unsafeAllocate(calculator2);
    const greaterThanOrEqualFn = greaterThanOrEqual$1(calculator2);
    const greaterThanFn = greaterThan$1(calculator2);
    const convertScaleFn = transformScale$1(calculator2);
    const maximumFn = maximum$1(calculator2);
    const equalFn = equal$2(calculator2);
    const zero2 = calculator2.zero();
    const ten = new Array(10).fill(null).reduce(calculator2.increment, zero2);
    return function allocate2(...[dineroObject, ratios]) {
      const hasRatios = ratios.length > 0;
      const scaledRatios = ratios.map((ratio) => getAmountAndScale(ratio, zero2));
      const highestRatioScale = hasRatios ? maximumFn(scaledRatios.map(({ scale: scale2 }) => scale2)) : zero2;
      const normalizedRatios = scaledRatios.map(({ amount, scale: scale2 }) => {
        const factor = equalFn(scale2, highestRatioScale) ? zero2 : calculator2.subtract(highestRatioScale, scale2);
        return {
          amount: calculator2.multiply(amount, calculator2.power(ten, factor)),
          scale: scale2
        };
      });
      const hasOnlyPositiveRatios = normalizedRatios.every(({ amount }) => greaterThanOrEqualFn(amount, zero2));
      const hasOneNonZeroRatio = normalizedRatios.some(({ amount }) => greaterThanFn(amount, zero2));
      assert(hasRatios && hasOnlyPositiveRatios && hasOneNonZeroRatio, INVALID_RATIOS_MESSAGE);
      const { scale } = dineroObject.toJSON();
      return allocateFn(convertScaleFn(dineroObject, calculator2.add(scale, highestRatioScale)), normalizedRatios);
    };
  }
  function multiply$1(calculator2) {
    const convertScaleFn = transformScale$1(calculator2);
    const zero2 = calculator2.zero();
    return function multiplyFn(...[multiplicand, multiplier]) {
      const { amount, currency, scale } = multiplicand.toJSON();
      const { amount: multiplierAmount, scale: multiplierScale } = getAmountAndScale(multiplier, zero2);
      const newScale = calculator2.add(scale, multiplierScale);
      return convertScaleFn(multiplicand.create({
        amount: calculator2.multiply(amount, multiplierAmount),
        currency,
        scale: newScale
      }), newScale);
    };
  }
  function toSnapshot$1(dineroObject) {
    return dineroObject.toJSON();
  }
  function allocate(...[dineroObject, ratios]) {
    const { calculator: calculator2 } = dineroObject;
    return safeAllocate(calculator2)(dineroObject, ratios);
  }
  function multiply(...[multiplicand, multiplier]) {
    const { calculator: calculator2 } = multiplicand;
    return multiply$1(calculator2)(multiplicand, multiplier);
  }
  var toSnapshot = toSnapshot$1;
  function transformScale(...[dineroObject, newScale, divide]) {
    const { calculator: calculator2 } = dineroObject;
    return transformScale$1(calculator2)(dineroObject, newScale, divide);
  }

  // node_modules/dinero.js/dist/esm/bigint/currencies/index.js
  var MYR = {
    code: "MYR",
    base: 10n,
    exponent: 2n
  };

  // node_modules/dinero.js/dist/esm/bigint/index.js
  var add$1 = (augend, addend) => {
    return augend + addend;
  };
  var compare$1 = (a, b) => {
    if (a < b) return DineroComparisonOperator.LT;
    if (a > b) return DineroComparisonOperator.GT;
    return DineroComparisonOperator.EQ;
  };
  var decrement = (value) => {
    return value - 1n;
  };
  var increment = (value) => {
    return value + 1n;
  };
  var integerDivide = (dividend, divisor) => {
    return dividend / divisor;
  };
  var modulo = (dividend, divisor) => {
    return dividend % divisor;
  };
  var multiply$12 = (multiplicand, multiplier) => {
    return multiplicand * multiplier;
  };
  var power = (base, exponent) => {
    return base ** exponent;
  };
  var subtract$1 = (minuend, subtrahend) => {
    return minuend - subtrahend;
  };
  function zero() {
    return 0n;
  }
  var calculator = {
    add: add$1,
    compare: compare$1,
    decrement,
    increment,
    integerDivide,
    modulo,
    multiply: multiply$12,
    power,
    subtract: subtract$1,
    zero
  };
  var dinero = createDinero({ calculator });

  // src/utils/money.ts
  function safeSen(value, fieldName) {
    if (value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new Error(`${fieldName} exceeds the safe sen range`);
    }
    return Number(value);
  }
  function requireSen(value, fieldName) {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new Error(`${fieldName} must be non-negative safe integer sen`);
    }
  }
  function scaledDecimal(value, fieldName) {
    if (!Number.isFinite(value) || value < 0) {
      throw new Error(`${fieldName} must be a finite non-negative number`);
    }
    const match = /^(\d+)(?:\.(\d+))?(?:e([+-]?\d+))?$/i.exec(value.toString());
    if (!match) throw new Error(`${fieldName} must be a finite non-negative decimal`);
    const fraction = match[2] ?? "";
    const exponent = Number(match[3] ?? "0");
    if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) {
      throw new Error(`${fieldName} exponent is outside the supported range`);
    }
    const amount = BigInt(`${match[1]}${fraction}`);
    const scale = fraction.length - exponent;
    return scale < 0 ? { amount: amount * 10n ** BigInt(-scale), scale: 0n } : { amount, scale: BigInt(scale) };
  }
  function decimalMoneyToSen(value, fieldName) {
    const money = dinero({ ...scaledDecimal(value, fieldName), currency: MYR });
    return safeSen(toSnapshot(transformScale(money, 2n, halfUp)).amount, fieldName);
  }
  function percentageDiscountSen(basePriceSen, percentage) {
    requireSen(basePriceSen, "promotion base price");
    const { amount, scale } = scaledDecimal(percentage, "promotion discountValue");
    const discount = multiply(dinero({ amount: BigInt(basePriceSen), currency: MYR }), {
      amount,
      scale: scale + 2n
    });
    return Math.min(basePriceSen, safeSen(
      toSnapshot(transformScale(discount, 2n, halfUp)).amount,
      "promotion percentage discount"
    ));
  }
  function allocateSen(amountSen, capacities) {
    requireSen(amountSen, "promotion allocation");
    capacities.forEach((capacity2) => requireSen(capacity2, "promotion allocation capacity"));
    const ratios = capacities.map(BigInt);
    const capacity = ratios.reduce((sum, ratio) => sum + ratio, 0n);
    if (BigInt(amountSen) > capacity) throw new Error("promotion allocation exceeds capacity");
    if (amountSen === 0) return capacities.map(() => 0);
    return allocate(dinero({ amount: BigInt(amountSen), currency: MYR }), ratios).map((part) => safeSen(toSnapshot(part).amount, "promotion allocation"));
  }

  // src/services/promotions/engine.ts
  var WARNING_AGE_MS = 4 * 60 * 60 * 1e3;
  var RESTRICTED_AGE_MS = 24 * 60 * 60 * 1e3;
  var LOCALLY_SUPPORTED_PROMOTION_TYPES = /* @__PURE__ */ new Set([
    "nth_item_discount",
    "buy_x_get_y",
    "item_discount",
    "happy_hour",
    "order_discount"
  ]);
  function parseOptionalRuleTime(value) {
    if (value == null) {
      return null;
    }
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : Number.NaN;
  }
  function evaluatePromotionLocalAvailability(rule, input) {
    if (rule.schedule != null && input.scheduleVersion !== 2) {
      return { available: false, reason: "unsupported_type" };
    }
    if (!rule.offlineAllowed) {
      return { available: false, reason: "online_only" };
    }
    if (!LOCALLY_SUPPORTED_PROMOTION_TYPES.has(rule.promotionType)) {
      return { available: false, reason: "unsupported_type" };
    }
    if (rule.posProfile && rule.posProfile !== input.posProfile) {
      return { available: false, reason: "pos_profile_mismatch" };
    }
    if ((rule.selectedPosProfiles?.length ?? 0) > 0 && !rule.selectedPosProfiles?.includes(input.posProfile)) {
      return { available: false, reason: "pos_profile_mismatch" };
    }
    const nowMs = Date.parse(input.nowIso);
    if (!Number.isFinite(nowMs)) {
      return { available: false, reason: "invalid_device_time" };
    }
    const validFromMs = parseOptionalRuleTime(rule.validFrom);
    if (Number.isNaN(validFromMs)) {
      return { available: false, reason: "invalid_valid_from" };
    }
    const validUptoMs = parseOptionalRuleTime(rule.validUpto);
    if (Number.isNaN(validUptoMs)) {
      return { available: false, reason: "invalid_valid_upto" };
    }
    if (validFromMs != null && nowMs < validFromMs) {
      return { available: false, reason: "not_yet_active" };
    }
    if (validUptoMs != null && nowMs > validUptoMs) {
      return { available: false, reason: "expired" };
    }
    return { available: true, reason: "available" };
  }
  function evaluatePromotionSyncStatus(snapshot, nowIso, optionsOrWarningAgeMs = {}, legacyRestrictedAgeMs = RESTRICTED_AGE_MS) {
    const options = typeof optionsOrWarningAgeMs === "number" ? {
      warningAgeMs: optionsOrWarningAgeMs,
      restrictedAgeMs: legacyRestrictedAgeMs
    } : optionsOrWarningAgeMs;
    if (!snapshot) {
      return {
        mode: "missing",
        ageMs: null,
        message: "Promos are not available yet"
      };
    }
    if (options.timeRestrictionReason) {
      return {
        mode: "restricted",
        ageMs: null,
        message: options.timeRestrictionReason === "clock_rollback" ? "Tablet time needs checking \xB7 promos are paused \xB7 sales can continue" : "Tablet time could not be checked \xB7 promos are paused \xB7 sales can continue",
        restrictionReason: options.timeRestrictionReason
      };
    }
    const downloadedAtMs = Date.parse(snapshot.downloadedAt);
    const nowMs = Date.parse(nowIso);
    if (!Number.isFinite(nowMs)) {
      return {
        mode: "restricted",
        ageMs: null,
        message: "Tablet time needs checking \xB7 promos are paused \xB7 sales can continue",
        restrictionReason: "invalid_device_time"
      };
    }
    if (!Number.isFinite(downloadedAtMs)) {
      return {
        mode: "missing",
        ageMs: null,
        message: "Promos could not be checked \xB7 sales use saved menu prices"
      };
    }
    if (downloadedAtMs - nowMs > PROMOTION_TIME_ROLLBACK_TOLERANCE_MS) {
      return {
        mode: "restricted",
        ageMs: null,
        message: "Tablet time needs checking \xB7 promos are paused \xB7 sales can continue",
        restrictionReason: "clock_rollback"
      };
    }
    const ageMs = Math.max(0, nowMs - downloadedAtMs);
    const restrictedAgeMs = options.restrictedAgeMs ?? RESTRICTED_AGE_MS;
    if (ageMs > restrictedAgeMs) {
      return {
        mode: "restricted",
        ageMs,
        message: "Promos may be out of date \xB7 sales continue with saved menu prices"
      };
    }
    const warningAgeMs = options.warningAgeMs ?? WARNING_AGE_MS;
    if (ageMs > warningAgeMs) {
      return {
        mode: "warning",
        ageMs,
        message: "Promos may need an update"
      };
    }
    return {
      mode: "normal",
      ageMs,
      message: "Promos are up to date"
    };
  }

  // src/services/promotions/schedule.ts
  var DAY = 864e5;
  var MALAYSIA = 8 * 60 * 6e4;
  var FIELDS = ["timeZone", "weekdays", "startDate", "endDate", "startMinute", "endMinute"];
  function record(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Choose a valid weekly schedule");
    return value;
  }
  function dateDay(value) {
    if (value === null) return null;
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("Choose a date using the calendar");
    const ms = Date.parse(value + "T00:00:00Z");
    if (!Number.isFinite(ms) || new Date(ms).toISOString().slice(0, 10) !== value) throw new Error("Choose a valid calendar date");
    return ms / DAY;
  }
  function readPromotionSchedule(value) {
    const row = record(value);
    if (Object.keys(row).length !== FIELDS.length || FIELDS.some((key) => !Object.prototype.hasOwnProperty.call(row, key))) {
      throw new Error("Weekly schedule fields are incomplete or unsupported");
    }
    if (row.timeZone !== "Asia/Kuala_Lumpur") throw new Error("Offers must use Malaysia time");
    const start = dateDay(row.startDate);
    const end = dateDay(row.endDate);
    if (start !== null && end !== null && start > end) throw new Error("The end date must not be before the start date");
    const days = row.weekdays;
    if (!Array.isArray(days) || !days.length || days.length > 7 || days.some((day) => typeof day !== "number" || !Number.isInteger(day) || day < 1 || day > 7) || new Set(days).size !== days.length) throw new Error("Choose at least one distinct weekday");
    const startMinute = row.startMinute;
    const endMinute = row.endMinute;
    if (typeof startMinute !== "number" || !Number.isInteger(startMinute) || startMinute < 0 || startMinute > 1439 || typeof endMinute !== "number" || !Number.isInteger(endMinute) || endMinute < 0 || endMinute > 1440 || startMinute === endMinute) throw new Error("Choose different start and end times, or select All day");
    return {
      timeZone: "Asia/Kuala_Lumpur",
      weekdays: [...days].sort((a, b) => a - b),
      startDate: start === null ? null : String(row.startDate),
      endDate: end === null ? null : String(row.endDate),
      startMinute,
      endMinute
    };
  }
  function instant(value) {
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
    const ms = Date.parse(value);
    if (!match || !Number.isFinite(ms) || +match[2] > 23 || +match[3] > 59 || +match[4] > 59 || +(match[6] ?? 0) > 23 || +(match[7] ?? 0) > 59) throw new Error("Schedule evaluation needs a valid time with its UTC offset");
    dateDay(match[1]);
    return ms;
  }
  function occurrence(schedule, day) {
    const first = dateDay(schedule.startDate);
    const last = dateDay(schedule.endDate);
    const weekday = ((day + 3) % 7 + 7) % 7 + 1;
    if (first !== null && day < first || last !== null && day > last || !schedule.weekdays.includes(weekday)) return null;
    const start = day * DAY - MALAYSIA + schedule.startMinute * 6e4;
    const end = day * DAY - MALAYSIA + schedule.endMinute * 6e4 + (schedule.endMinute < schedule.startMinute ? DAY : 0);
    return { start, end };
  }
  function promotionScheduleContains(scheduleValue, nowIso) {
    const schedule = readPromotionSchedule(scheduleValue);
    const now = instant(nowIso);
    const day = Math.floor((now + MALAYSIA) / DAY);
    return [day - 1, day].some((candidate2) => {
      const window = occurrence(schedule, candidate2);
      return window !== null && now >= window.start && now < window.end;
    });
  }
  function nextPromotionOccurrences(scheduleValue, nowIso, count = 3) {
    const schedule = readPromotionSchedule(scheduleValue);
    if (!Number.isInteger(count) || count < 1 || count > 7) throw new Error("Request between one and seven upcoming occurrences");
    const now = instant(nowIso);
    const firstDay = Math.max(Math.floor((now + MALAYSIA) / DAY) - 1, dateDay(schedule.startDate) ?? -Infinity);
    const result = [];
    for (let day = firstDay; day < firstDay + 8 * 7 && result.length < count; day++) {
      const window = occurrence(schedule, day);
      if (window && window.end > now) result.push({ start: new Date(window.start).toISOString(), end: new Date(window.end).toISOString() });
    }
    return result;
  }

  // src/services/promotions/kernel.ts
  var PROMOTION_KERNEL_LIMITS = Object.freeze({
    lines: 100,
    units: 1e3,
    matchingUnits: 250,
    rules: 50,
    candidates: 256,
    generationSteps: 5e4,
    searchNodes: 5e4
  });
  var EvaluationLimit = class extends Error {
    constructor(reason) {
      super(reason);
      __publicField(this, "reason", reason);
    }
  };
  var compareText = (left, right) => left < right ? -1 : left > right ? 1 : 0;
  var compareRules = (left, right) => left.priority < right.priority ? -1 : left.priority > right.priority ? 1 : compareText(left.promotionId, right.promotionId);
  function integer(value, field, minimum2 = 0) {
    if (!Number.isSafeInteger(value) || value < minimum2) throw new Error(`${field} must be a safe integer >= ${minimum2}`);
    return value;
  }
  function identifier(value, field) {
    if (typeof value !== "string" || !value.trim() || value.length > 256) {
      throw new Error(`${field} must be a non-empty identifier of at most 256 characters`);
    }
  }
  function sumSen(values) {
    return integer(Number(values.reduce((sum, value) => sum + BigInt(value), 0n)), "promotion amount sum");
  }
  function ruleBasis(rule, unit) {
    return unit.line.unitPriceSen + (rule.modifierPolicy === "include_paid_modifiers" ? unit.line.modifierTotalSen : 0);
  }
  function validateRule(rule) {
    if (rule.schedule != null) readPromotionSchedule(rule.schedule);
    identifier(rule.promotionId, "promotion ID");
    integer(rule.priority, "promotion priority", -Number.MAX_SAFE_INTEGER);
    const enums = [
      [rule.promotionType, PROMOTION_FIELD_ENUMS.promotion_type, "type"],
      [rule.activationMode, PROMOTION_FIELD_ENUMS.activation_mode, "activation mode"],
      [rule.stackingPolicy, PROMOTION_FIELD_ENUMS.stacking_policy, "stacking policy"],
      [rule.discountType, PROMOTION_FIELD_ENUMS.discount_type, "discount type"],
      [rule.discountTarget, PROMOTION_FIELD_ENUMS.discount_target, "discount target"],
      [rule.modifierPolicy, PROMOTION_FIELD_ENUMS.modifier_policy, "modifier policy"],
      [rule.eligibleScopeMode, PROMOTION_FIELD_ENUMS.eligible_scope_mode, "eligible scope"],
      [rule.repeatMode, PROMOTION_FIELD_ENUMS.repeat_mode, "repeat mode"],
      [rule.comparisonBasis, PROMOTION_FIELD_ENUMS.comparison_basis, "comparison basis"],
      [rule.discountBasis, PROMOTION_FIELD_ENUMS.discount_basis, "discount basis"]
    ];
    for (const [value, allowed, field] of enums) {
      if (value !== void 0 && !allowed.includes(value)) throw new Error(`promotion ${field} is unsupported`);
    }
    for (const [field, value] of [["buy quantity", rule.buyQty], ["discount quantity", rule.discountQty], ["minimum quantity", rule.minQty]]) {
      if (value !== void 0) integer(value, `promotion ${field}`);
    }
    for (const values of [rule.eligibleItems, rule.eligibleItemGroups, rule.selectedPosProfiles]) {
      if ((values?.length ?? 0) > 1e4) throw new EvaluationLimit("input_limit");
      values?.forEach((value) => identifier(value, "promotion eligibility ID"));
    }
    if (!Number.isFinite(rule.discountValue ?? 0) || (rule.discountValue ?? 0) < 0) {
      throw new Error("promotion discount value must be finite and non-negative");
    }
    decimalMoneyToSen(rule.minAmount ?? 0, "promotion minimum amount");
  }
  function expandUnits(input) {
    const menu = new Map(input.menuItems.map((item) => [item.id, item]));
    const frozen = input.snapshot?.kernelVersion === 2 ? new Map((input.snapshot.frozenItems ?? []).map((item) => [item.itemCode, item.itemGroup])) : null;
    const units = [];
    const seen = /* @__PURE__ */ new Set();
    let gross = 0n;
    const lines = [...input.cartItems].sort((a, b) => compareText(a.sku ?? a.menuItemId, b.sku ?? b.menuItemId) || a.unitPriceSen - b.unitPriceSen || a.modifierTotalSen - b.modifierTotalSen || a.quantity - b.quantity || a.discountAmountSen - b.discountAmountSen);
    for (const line of lines) {
      identifier(line.id, "cart line ID");
      if (seen.has(line.id)) throw new Error("promotion cart line IDs must be unique");
      seen.add(line.id);
      integer(line.quantity, "promotion line quantity", 1);
      integer(line.unitPriceSen, "promotion unit price");
      integer(line.modifierTotalSen, "promotion modifier total");
      integer(line.discountAmountSen, "promotion manual discount");
      if (units.length + line.quantity > PROMOTION_KERNEL_LIMITS.units) throw new EvaluationLimit("input_limit");
      if (line.promotionAllocations?.length) throw new Error("promotion quote requires an unpriced cart");
      const unitGross = sumSen([line.unitPriceSen, line.modifierTotalSen]);
      const lineGross = integer(Number(BigInt(unitGross) * BigInt(line.quantity)), "promotion line gross");
      gross += BigInt(lineGross);
      if (gross > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("promotion cart gross exceeds safe sen range");
      const manual = allocateSen(line.discountAmountSen, Array(line.quantity).fill(unitGross));
      for (let n = 0; n < line.quantity; n++) {
        const index = units.length;
        units.push({
          index,
          line,
          group: (frozen ? frozen.get(line.menuItemId) : menu.get(line.menuItemId)?.categoryId) ?? "",
          capacity: unitGross - manual[n],
          baseCapacity: Math.max(0, line.unitPriceSen - manual[n]),
          bit: 1n << BigInt(index)
        });
      }
    }
    return units;
  }
  function ruleContexts(input, units) {
    const seen = /* @__PURE__ */ new Set();
    const menu = new Set(input.snapshot?.kernelVersion === 2 ? (input.snapshot.frozenItems ?? []).map((item) => item.itemCode) : input.menuItems.map((item) => item.id));
    const selected = new Set(input.selectedPromotionIds ?? []);
    return [...input.snapshot?.promotions ?? []].sort(compareRules).flatMap((rule, index) => {
      validateRule(rule);
      if (seen.has(rule.promotionId)) throw new Error("promotion IDs must be unique");
      seen.add(rule.promotionId);
      if (!evaluatePromotionLocalAvailability(rule, { ...input, scheduleVersion: 2 }).available || rule.schedule != null && !promotionScheduleContains(rule.schedule, input.nowIso) || rule.activationMode === "manual_selectable" && !selected.has(rule.promotionId)) return [];
      const eligible = units.filter((unit) => menu.has(unit.line.menuItemId) && (!rule.eligibleItems?.length || rule.eligibleItems.includes(unit.line.menuItemId)) && (!rule.eligibleItemGroups?.length || rule.eligibleItemGroups.includes(unit.group)));
      if (eligible.length < (rule.minQty ?? 0) || sumSen(eligible.map((unit) => unit.line.unitPriceSen)) < decimalMoneyToSen(rule.minAmount ?? 0, "promotion minimum amount")) return [];
      const cache = /* @__PURE__ */ new Map();
      const fixed = rule.discountType === "fixed_price" || rule.discountType === "fixed_amount" ? decimalMoneyToSen(rule.discountValue ?? 0, "promotion fixed discount value") : 0;
      return [{ rule, index, units: eligible, discount: (base) => {
        const cached = cache.get(base);
        if (cached !== void 0) return cached;
        const amount = rule.discountType === "fixed_amount" ? Math.min(base, fixed) : rule.discountType === "fixed_price" ? Math.max(0, base - fixed) : rule.discountType === "free_item" ? base : percentageDiscountSen(base, rule.discountValue ?? 0);
        cache.set(base, amount);
        return amount;
      } }];
    });
  }
  function unitDiscountSen(context, unit) {
    return Math.min(
      context.rule.modifierPolicy === "include_paid_modifiers" ? unit.capacity : unit.baseCapacity,
      context.discount(ruleBasis(context.rule, unit))
    );
  }
  function candidate(context, consumed, targets, once = null) {
    const credits = targets.map((unit) => ({ unit: unit.index, amount: unitDiscountSen(context, unit) }));
    return { rule: context, mask: consumed.reduce((mask, unit) => mask | unit.bit, 0n), credits, once, amount: sumSen(credits.map((credit) => credit.amount)) };
  }
  function enumerateCandidates(contexts) {
    const candidates = [];
    let steps = 0;
    const add2 = (entry) => {
      if (entry.amount > 0) candidates.push(entry);
      if (candidates.length > PROMOTION_KERNEL_LIMITS.candidates) throw new EvaluationLimit("candidate_limit");
    };
    const ordered = [
      ...contexts.filter(({ rule }) => rule.promotionType !== "order_discount"),
      ...contexts.filter(({ rule }) => rule.promotionType === "order_discount")
    ];
    for (const context of ordered) {
      const { rule, units } = context;
      if (rule.promotionType === "order_discount") {
        const capacities = units.map((unit) => rule.modifierPolicy === "include_paid_modifiers" ? unit.capacity : unit.baseCapacity);
        const amount = Math.min(sumSen(capacities), context.discount(sumSen(units.map((unit) => ruleBasis(rule, unit)))));
        const shares = allocateSen(amount, capacities);
        const entry = candidate(context, units, []);
        entry.credits = units.map((unit, i) => ({ unit: unit.index, amount: shares[i] }));
        entry.amount = amount;
        add2(entry);
      } else if (rule.promotionType === "buy_x_get_y" || rule.promotionType === "nth_item_discount") {
        const get = Math.max(1, rule.discountQty ?? 1);
        const size = Math.max(1, rule.buyQty ?? 1) + get;
        const pools = /* @__PURE__ */ new Map();
        for (const unit of units) {
          const key = rule.eligibleScopeMode === "same_item" ? unit.line.menuItemId : "";
          const pool = pools.get(key) ?? [];
          pool.push(unit);
          pools.set(key, pool);
        }
        for (const [key, pool] of pools) {
          if (pool.length < size) continue;
          const indexes = Array.from({ length: size }, (_, i) => i);
          while (true) {
            steps += size;
            if (steps > PROMOTION_KERNEL_LIMITS.generationSteps) throw new EvaluationLimit("candidate_limit");
            const chosen = indexes.map((i) => pool[i]);
            const targets = [...chosen].sort((a, b) => a.line.unitPriceSen - b.line.unitPriceSen || a.index - b.index).slice(0, get);
            add2(candidate(context, chosen, targets, rule.repeatMode === "repeat" ? null : JSON.stringify([context.index, key])));
            let next = size - 1;
            while (next >= 0 && indexes[next] === pool.length - size + next) next--;
            if (next < 0) break;
            indexes[next]++;
            for (let i = next + 1; i < size; i++) indexes[i] = indexes[i - 1] + 1;
          }
        }
      } else {
        units.forEach((unit) => add2(candidate(context, [unit], [unit])));
      }
    }
    return candidates;
  }
  function solveIndependentItems(contexts, units) {
    const byUnit = units.map(() => ({ bestIndex: -1, bestAmount: 0, excluded: 0, included: 0 }));
    for (const context of contexts) {
      for (const unit of context.units) {
        const amount = unitDiscountSen(context, unit);
        if (amount <= 0) continue;
        const choices = byUnit[unit.index];
        if (context.rule.stackingPolicy === "stackable") {
          if (context.rule.modifierPolicy === "include_paid_modifiers") {
            choices.included = Math.min(unit.capacity, choices.included + amount);
          } else choices.excluded = Math.min(unit.baseCapacity, choices.excluded + amount);
        } else if (amount > choices.bestAmount) {
          choices.bestIndex = context.index;
          choices.bestAmount = amount;
        }
      }
    }
    const selected = [];
    for (const context of contexts) {
      const entry = { rule: context, mask: 0n, credits: [], once: null, amount: 0 };
      for (const unit of context.units) {
        const choices = byUnit[unit.index];
        const bestWins = choices.bestAmount > Math.min(unit.capacity, choices.excluded + choices.included);
        if (bestWins ? choices.bestIndex !== context.index : context.rule.stackingPolicy !== "stackable") continue;
        const amount = unitDiscountSen(context, unit);
        if (amount > 0) {
          entry.credits.push({ unit: unit.index, amount });
          entry.amount += amount;
        }
      }
      if (entry.amount > 0) selected.push(entry);
    }
    return selected;
  }
  function solveSingleMonotoneBogo(context) {
    const { rule, units } = context;
    if (rule.promotionType !== "buy_x_get_y" && rule.promotionType !== "nth_item_discount") return null;
    if (units.some((unit) => unit.baseCapacity !== unit.line.unitPriceSen) || rule.modifierPolicy === "include_paid_modifiers" && units.some((unit) => unit.line.modifierTotalSen !== units[0]?.line.modifierTotalSen)) return null;
    const get = Math.max(1, rule.discountQty ?? 1);
    const size = Math.max(1, rule.buyQty ?? 1) + get;
    const pools = /* @__PURE__ */ new Map();
    for (const unit of units) {
      const key = rule.eligibleScopeMode === "same_item" ? unit.line.menuItemId : "";
      const pool = pools.get(key) ?? [];
      pool.push(unit);
      pools.set(key, pool);
    }
    const result = [];
    for (const pool of pools.values()) {
      pool.sort((a, b) => b.line.unitPriceSen - a.line.unitPriceSen || a.index - b.index);
      const cycles = Math.min(Math.floor(pool.length / size), rule.repeatMode === "repeat" ? units.length : 1);
      for (let n = 0; n < cycles; n++) {
        const chosen = pool.slice(n * size, (n + 1) * size);
        const targets = [...chosen].sort((a, b) => a.line.unitPriceSen - b.line.unitPriceSen || a.index - b.index).slice(0, get);
        const entry = candidate(context, chosen, targets);
        if (entry.amount > 0) result.push(entry);
      }
    }
    return result;
  }
  function solveSimplePairs(contexts, units, onNode) {
    const pair = (rule) => rule.promotionType === "buy_x_get_y" || rule.promotionType === "nth_item_discount";
    if (contexts.some(({ rule }) => rule.stackingPolicy === "stackable" || (pair(rule) ? rule.repeatMode !== "repeat" || Math.max(1, rule.buyQty ?? 1) !== 1 || Math.max(1, rule.discountQty ?? 1) !== 1 : !["item_discount", "happy_hour"].includes(rule.promotionType)))) return null;
    const eligible = new Map(contexts.map((context) => [context.index, new Set(context.units.map((unit) => unit.index))]));
    const singles = units.map((unit) => {
      let best = null;
      for (const context of contexts) {
        if (!pair(context.rule) && eligible.get(context.index)?.has(unit.index)) {
          const amount = unitDiscountSen(context, unit);
          if (amount > (best?.amount ?? 0)) best = candidate(context, [unit], [unit]);
        }
      }
      return best;
    });
    const singleEntries = () => singles.filter((entry) => entry !== null);
    const pairs = contexts.filter((context) => pair(context.rule) && context.units.length > 1 && Math.max(...context.units.map((unit) => unitDiscountSen(context, unit))) > 2 * Math.min(...context.units.map((unit) => singles[unit.index]?.amount ?? 0)));
    if (!pairs.length || units.length < 2) return singleEntries();
    const bestPair = (left, right) => {
      const target = left.line.unitPriceSen <= right.line.unitPriceSen ? left : right;
      let best = null;
      for (const context of pairs) {
        const allowed = eligible.get(context.index);
        if (!allowed?.has(left.index) || !allowed.has(right.index) || context.rule.eligibleScopeMode === "same_item" && left.line.menuItemId !== right.line.menuItemId) continue;
        const amount = unitDiscountSen(context, target);
        if (amount > (best?.amount ?? 0)) best = candidate(context, [left, right], [target]);
      }
      return best;
    };
    const signature = (unit) => JSON.stringify([
      unit.line.unitPriceSen,
      unit.line.modifierTotalSen,
      unit.capacity,
      unit.baseCapacity,
      singles[unit.index]?.amount ?? 0,
      pairs.map((context) => !eligible.get(context.index)?.has(unit.index) ? null : context.rule.eligibleScopeMode === "same_item" ? unit.line.menuItemId : true)
    ]);
    if (units.every((unit) => signature(unit) === signature(units[0]))) {
      const best = bestPair(units[0], units[1]);
      if (!best || best.amount <= (singles[0]?.amount ?? 0) + (singles[1]?.amount ?? 0)) return singleEntries();
      const selected2 = [];
      for (let i = 0; i + 1 < units.length; i += 2) selected2.push(candidate(best.rule, [units[i], units[i + 1]], [units[i]]));
      if (units.length % 2 && singles[units.length - 1]) selected2.push(singles[units.length - 1]);
      return selected2;
    }
    if (units.length > PROMOTION_KERNEL_LIMITS.matchingUnits) throw new EvaluationLimit("input_limit");
    const edges = [];
    const choices = /* @__PURE__ */ new Map();
    for (let i = 0; i < units.length; i++) for (let j = i + 1; j < units.length; j++) {
      onNode();
      const entry = bestPair(units[i], units[j]);
      const gain = (entry?.amount ?? 0) - (singles[i]?.amount ?? 0) - (singles[j]?.amount ?? 0);
      if (!entry || gain <= 0) continue;
      if (!Number.isSafeInteger(gain) || gain > Math.floor(Number.MAX_SAFE_INTEGER / 4)) throw new EvaluationLimit("input_limit");
      edges.push([i, j, gain]);
      choices.set(i * units.length + j, entry);
    }
    const matching = (0, import_edmonds_blossom_fixed.default)(edges, false);
    const selected = [];
    for (let i = 0; i < units.length; i++) {
      const mate = matching[i] ?? -1;
      if (mate === -1) {
        if (singles[i]) selected.push(singles[i]);
        continue;
      }
      if (!Number.isInteger(mate) || mate === i || mate < 0 || mate >= units.length || matching[mate] !== i) {
        throw new Error("The matching library returned an invalid promotion allocation");
      }
      const entry = choices.get(Math.min(i, mate) * units.length + Math.max(i, mate));
      if (!entry) throw new Error("The matching library selected an unknown promotion pair");
      if (mate > i) selected.push(entry);
    }
    return selected;
  }
  function search(candidates, units, onNode) {
    const excluded = units.map(() => 0);
    const included = units.map(() => 0);
    const used = (index) => Math.min(units[index].capacity, excluded[index] + included[index]);
    const perRule = /* @__PURE__ */ new Map();
    const once = /* @__PURE__ */ new Set();
    const selected = [];
    let best = [];
    let bestAmount = -1;
    const capacity = sumSen(units.map((unit) => unit.capacity));
    const suffix = Array(candidates.length + 1).fill(0);
    for (let i = candidates.length - 1; i >= 0; i--) {
      suffix[i] = candidates[i].amount >= capacity - suffix[i + 1] ? capacity : candidates[i].amount + suffix[i + 1];
    }
    const visit = (i, amount, allMask, exclusiveMask) => {
      onNode();
      if (amount > bestAmount) {
        bestAmount = amount;
        best = [...selected];
      }
      if (i === candidates.length || bestAmount === capacity || suffix[i] <= bestAmount - amount) return;
      const entry = candidates[i];
      const ruleIndex = entry.rule.index;
      const previous = perRule.get(ruleIndex) ?? 0n;
      const stackable = entry.rule.rule.stackingPolicy === "stackable";
      const conflicts = (entry.mask & (stackable ? exclusiveMask : allMask)) !== 0n || (entry.mask & previous) !== 0n || entry.once !== null && once.has(entry.once);
      if (!conflicts) {
        const includeModifiers = entry.rule.rule.modifierPolicy === "include_paid_modifiers";
        let credits = entry.credits;
        if (entry.rule.rule.promotionType === "order_discount") {
          const headroom = entry.rule.units.map((unit) => Math.min(
            unit.capacity - used(unit.index),
            includeModifiers ? unit.capacity : unit.baseCapacity - excluded[unit.index]
          ));
          const shares = allocateSen(Math.min(entry.amount, sumSen(headroom)), headroom);
          credits = entry.rule.units.map((unit, n) => ({ unit: unit.index, amount: shares[n] }));
        }
        const previousMoney = credits.map(({ unit }) => ({ unit, excluded: excluded[unit], included: included[unit], total: used(unit) }));
        credits.forEach(({ unit, amount: raw }) => {
          if (includeModifiers) included[unit] = Math.min(units[unit].capacity, included[unit] + raw);
          else excluded[unit] = Math.min(units[unit].baseCapacity, excluded[unit] + raw);
        });
        const delta = sumSen(previousMoney.map((previous2) => used(previous2.unit) - previous2.total));
        if (delta > 0) {
          perRule.set(ruleIndex, previous | entry.mask);
          if (entry.once !== null) once.add(entry.once);
          selected.push({ ...entry, credits });
          visit(i + 1, amount + delta, allMask | entry.mask, stackable ? exclusiveMask : exclusiveMask | entry.mask);
          selected.pop();
          if (entry.once !== null) once.delete(entry.once);
          perRule.set(ruleIndex, previous);
        }
        previousMoney.forEach((previous2) => {
          excluded[previous2.unit] = previous2.excluded;
          included[previous2.unit] = previous2.included;
        });
      }
      visit(i + 1, amount, allMask, exclusiveMask);
    };
    visit(0, 0, 0n, 0n);
    return best;
  }
  function complete(input, units, selected, searchNodes) {
    const used = units.map(() => 0);
    const applied = /* @__PURE__ */ new Map();
    const lineAllocations = /* @__PURE__ */ new Map();
    for (const entry of [...selected].sort((a, b) => Number(a.rule.rule.modifierPolicy === "include_paid_modifiers") - Number(b.rule.rule.modifierPolicy === "include_paid_modifiers") || a.rule.index - b.rule.index)) {
      const rule = entry.rule.rule;
      let entryAmount = 0;
      for (const { unit: index, amount: raw } of entry.credits) {
        const unit = units[index];
        const capacity = rule.modifierPolicy === "include_paid_modifiers" ? unit.capacity : unit.baseCapacity;
        const amount = Math.min(raw, capacity - used[index]);
        if (amount <= 0) continue;
        used[index] += amount;
        entryAmount += amount;
        let line = lineAllocations.get(unit.line.id);
        if (!line) {
          line = /* @__PURE__ */ new Map();
          lineAllocations.set(unit.line.id, line);
        }
        let allocation = line.get(rule.promotionId);
        if (!allocation) {
          allocation = { amountSen: 0, quantity: 0 };
          line.set(rule.promotionId, allocation);
        }
        allocation.amountSen += amount;
        allocation.quantity++;
      }
      if (entryAmount > 0) {
        const promo = applied.get(rule.promotionId) ?? { promotionId: rule.promotionId, promotionName: rule.promotionName, promotionType: rule.promotionType, amountSen: 0 };
        promo.amountSen = (promo.amountSen ?? 0) + entryAmount;
        applied.set(rule.promotionId, promo);
      }
    }
    return {
      kernelVersion: 2,
      status: "complete",
      searchNodes,
      totalDiscountSen: sumSen(used),
      appliedPromotions: [...applied.values()],
      items: input.cartItems.map((line) => {
        const allocations = [...lineAllocations.get(line.id) ?? []].map(([promotionId, allocation]) => ({
          promotionId,
          amountSen: allocation.amountSen,
          quantity: allocation.quantity,
          scope: "line"
        }));
        const discountAmountSen = line.discountAmountSen + sumSen(allocations.map((allocation) => allocation.amountSen));
        return {
          ...line,
          modifiers: line.modifiers.map((modifier) => ({ ...modifier })),
          promotionAllocations: allocations,
          discountAmountSen,
          lineTotalSen: (line.unitPriceSen + line.modifierTotalSen) * line.quantity - discountAmountSen
        };
      })
    };
  }
  function quotePromotions(input) {
    if (input.kernelVersion !== 2) throw new Error("unsupported promotion kernel version");
    let searchNodes = 0;
    try {
      if (input.cartItems.length > PROMOTION_KERNEL_LIMITS.lines || (input.snapshot?.promotions.length ?? 0) > PROMOTION_KERNEL_LIMITS.rules || input.menuItems.length > 1e4 || (input.selectedPromotionIds?.length ?? 0) > PROMOTION_KERNEL_LIMITS.rules) throw new EvaluationLimit("input_limit");
      const status = evaluatePromotionSyncStatus(input.snapshot, input.nowIso, { timeRestrictionReason: input.timeRestrictionReason });
      if (!input.snapshot || input.snapshot.source === "demo" || status.mode === "missing" || status.mode === "restricted" || input.snapshot.posProfile !== input.posProfile) return { kernelVersion: 2, status: "incomplete", reason: "snapshot_unavailable", searchNodes };
      const units = expandUnits(input);
      const contexts = ruleContexts(input, units);
      const singleBogo = contexts.length === 1 ? solveSingleMonotoneBogo(contexts[0]) : null;
      const onNode = () => {
        if (++searchNodes > PROMOTION_KERNEL_LIMITS.searchNodes) throw new EvaluationLimit("search_limit");
      };
      const selected = singleBogo ?? (contexts.every(({ rule }) => rule.promotionType === "item_discount" || rule.promotionType === "happy_hour") ? solveIndependentItems(contexts, units) : solveSimplePairs(contexts, units, onNode) ?? search(enumerateCandidates(contexts), units, onNode));
      return complete(input, units, selected, searchNodes);
    } catch (error) {
      if (error instanceof EvaluationLimit) return { kernelVersion: 2, status: "incomplete", reason: error.reason, searchNodes };
      throw error;
    }
  }
  function quotePromotionBasketTotals(request) {
    if (!Array.isArray(request.baskets) || request.baskets.length > 1275) {
      throw new Error("Cost review accepts at most 1275 one- or two-item examples");
    }
    return request.baskets.map((basket) => {
      if (basket.length < 1 || basket.length > 2 || basket.some((line) => line.quantity !== 1)) {
        throw new Error("Cost review examples must contain one or two single units");
      }
      const result = quotePromotions({ ...request.input, cartItems: basket });
      if (result.status !== "complete") throw new Error("The cost review could not prove a complete basket price");
      return result.totalDiscountSen;
    });
  }

  // src/services/promotions/identity.ts
  var import_canonicalize = __toESM(require_canonicalize());
  function validString(value) {
    for (let i = 0; i < value.length; i++) {
      const code = value.charCodeAt(i);
      if (code >= 55296 && code <= 56319) {
        const next = value.charCodeAt(++i);
        if (!(next >= 56320 && next <= 57343)) throw new Error("Snapshot text contains an invalid Unicode character");
      } else if (code >= 56320 && code <= 57343) throw new Error("Snapshot text contains an invalid Unicode character");
    }
  }
  function canonicalPromotionContent(value) {
    let nodes = 0;
    function check(part, depth = 0) {
      if (++nodes > 1e5 || depth > 16) throw new Error("Promotion identity exceeds its structural limit");
      if (part === null || typeof part === "boolean") return;
      if (typeof part === "string") {
        validString(part);
        return;
      }
      if (typeof part === "number" && Number.isFinite(part)) return;
      if (Array.isArray(part)) {
        part.forEach((child) => check(child, depth + 1));
        return;
      }
      if (typeof part === "object" && (Object.getPrototypeOf(part) === Object.prototype || Object.getPrototypeOf(part) === null)) {
        Object.entries(part).forEach(([key, child]) => {
          validString(key);
          check(child, depth + 1);
        });
        return;
      }
      throw new Error("Promotion identity requires finite, plain JSON values");
    }
    check(value);
    const canonical = (0, import_canonicalize.default)(value);
    if (canonical === void 0 || canonical.length > 1048576) throw new Error("Promotion identity exceeds its size limit");
    return canonical;
  }
  return __toCommonJS(promotion_runtime_entry_exports);
})();

if (typeof module !== 'undefined') module.exports = JiJiPromotionRuntime;
