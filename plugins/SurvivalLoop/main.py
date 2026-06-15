
import heapq
import math
import os
import time

from plugin import Plugin
from skills import (
    chat,
    collect_blocks,
    craft,
    get_an_item_in_inventory,
    get_entity_position,
    get_inventory_counts,
    get_item_counts,
    get_nearest_block,
    get_nearest_blocks,
    go_to_position,
    pickup_nearby_items,
    place_block,
)
from utils import add_log, read_json, sizeof, write_json
from world import get_empty_block_names, get_item_id, get_wood_types, vec3


def survival_loop_step(agent):
    return agent.plugins["SurvivalLoop"].step(force=True)


def build_cliff_path(agent):
    return agent.plugins["SurvivalLoop"]._build_cliff_path_step()


def plant_saplings(agent):
    return agent.plugins["SurvivalLoop"]._plant_saplings_step()


def build_resource_path(agent):
    return agent.plugins["SurvivalLoop"]._build_resource_path_step()


def find_trees(agent):
    return agent.plugins["SurvivalLoop"]._find_trees()


def sense_terrain(agent):
    return agent.plugins["SurvivalLoop"]._sense_terrain()


def plan_resource_route(agent):
    return agent.plugins["SurvivalLoop"]._plan_resource_route_action()


def escape_stuck(agent):
    return agent.plugins["SurvivalLoop"]._escape_stuck_step()


def escape_cave(agent):
    return agent.plugins["SurvivalLoop"]._escape_cave_step()


class PluginInstance(Plugin):
    def __init__(self, agent):
        super().__init__(agent)
        self.plugin_name = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
        self.path = "./plugins/%s/" % self.plugin_name
        self.save_path = os.path.join(self.path, "save.json")
        self.last_tick = 0
        self.tick_interval = int(agent.configs.get("survival_loop_interval_seconds", 25))
        self.enabled = bool(agent.configs.get("survival_loop_enabled", True))
        self.state = {"base": None, "shelter_blocks_done": [], "farm_origin": None, "farm_blocks_done": [], "farm_failures": 0, "work_cursor": 0}
        self.load()
        self._upgrade_route_strategy_state()
        self._drop_failed_high_tree_routes()
        if not self.state.get("farm_mode"):
            self.state["farm_mode"] = "riverbank"
            self.save()
        if self.state.get("farm_height_mode") != "hydrated_daylight_expansion_v5":
            self.state["farm_height_mode"] = "hydrated_daylight_expansion_v5"
            self.state.pop("riverbank_farm_positions", None)
            self.state["farm_blocks_done"] = []
            self.state.pop("abandoned_farm_positions", None)
            self.state.pop("farm_expansion_plan", None)
            self.state.pop("cliff_path_plan", None)
            self.state["cliff_path_done"] = []
            self.save()

    def load(self):
        if os.path.isfile(self.save_path):
            try:
                data = read_json(self.save_path)
                if isinstance(data, dict):
                    self.state.update(data)
            except Exception:
                pass

    def save(self):
        write_json(self.state, self.save_path)

    def _upgrade_route_strategy_state(self):
        version = "pathfinder_cost_high_tree_v33"
        if self.state.get("resource_route_strategy_version") == version:
            return
        failures = dict(self.state.get("resource_path_plan_failures", {}) or {})
        failures = {key: value for key, value in failures.items() if not str(key).startswith("tree_high:")}
        self.state["resource_path_plan_failures"] = failures
        unreachable = self.state.get("unreachable_resource_targets", [])
        if isinstance(unreachable, list):
            self.state["unreachable_resource_targets"] = [
                item for item in unreachable
                if not (str(item).startswith("tree_high:") or str(item).startswith("tree_search:"))
            ]
        self.state["resource_route_strategy_version"] = version
        blocked = self.state.get("blocked_build_positions", [])
        if isinstance(blocked, list):
            self.state["blocked_build_positions"] = [
                item for item in blocked
                if not self._is_high_tree_route_block_key(item)
            ]
        build_failures = dict(self.state.get("build_position_failures", {}) or {})
        self.state["build_position_failures"] = {
            key: value for key, value in build_failures.items()
            if not self._is_high_tree_route_block_key(key)
        }
        for key in list(self.state.keys()):
            if str(key).startswith("resource_path_") and key != "resource_path_plan_failures":
                self.state.pop(key, None)
        self.save()

    def _is_high_tree_route_block_key(self, key):
        try:
            x, y, z = [int(part) for part in str(key).split(",")]
        except Exception:
            return False
        return y >= int(self._base().get("y", 65)) + 12

    def get_actions(self):
        return [{
            "name": "survival_loop_step",
            "description": "Run one conservative autonomous survival step: gather logs/dirt/seeds, craft supplies, build a compact shelter, and make progress on a small wheat farm.",
            "params": {},
            "perform": survival_loop_step,
        }, {
            "name": "build_cliff_path",
            "description": "Build one step of a dirt stair path along a cliff so the bot can climb up and down while hauling resources.",
            "params": {},
            "perform": build_cliff_path,
        }, {
            "name": "plant_saplings",
            "description": "Plant collected tree saplings with safe spacing, prioritizing the original tree trunk positions recorded after logging so wood remains renewable.",
            "params": {},
            "perform": plant_saplings,
        }, {
            "name": "build_resource_path",
            "description": "Build one dirt path segment toward trees or water so the bot can haul resources and develop farmland.",
            "params": {},
            "perform": build_resource_path,
        }, {
            "name": "find_trees",
            "description": "Travel along the dirt resource path toward a tree search target, then scan for logs.",
            "params": {},
            "perform": find_trees,
        }, {
            "name": "sense_terrain",
            "description": "Scan nearby 3D terrain and classify walkable ground, cliffs, shallow water, farm terraces, resource paths, and tree directions.",
            "params": {},
            "perform": sense_terrain,
        }, {
            "name": "plan_resource_route",
            "description": "Scan for nearby trees/resources, remember their positions, and plan a walkable or buildable route with one-block jumps.",
            "params": {},
            "perform": plan_resource_route,
        }, {
            "name": "escape_stuck",
            "description": "Emergency unstuck action: dig nearby hand-diggable blocks even without ideal tools, then jump or step out.",
            "params": {},
            "perform": escape_stuck,
        }, {
            "name": "escape_cave",
            "description": "Move out of a stone cave before searching for trees; avoid digging stone and prefer open air or dirt-supported exits.",
            "params": {},
            "perform": escape_cave,
        }]

    def get_reminder(self):
        return "SurvivalLoop is enabled. Prefer survival_loop_step for steady resource gathering, shelter building, farming, harvesting, and replanting near spawn. Forestry rule: after cutting logs, collect saplings and plant them, preferably at the original trunk site or at a safely spaced nearby site."

    def _report(self, message, label="plugin"):
        add_log(title=self.pack_message("Survival loop report."), content=message, label=label)
        if self.agent.configs.get("survival_loop_chat_enabled", False):
            chat(self.agent, "", message)

    def survival_tick(self):
        if not self.enabled:
            return False
        now = time.time()
        if now - self.last_tick < self.tick_interval:
            return False
        if self.agent.working_process is not None:
            return False
        if getattr(self.agent, "executor", None) is not None and self.agent.executor.is_busy:
            return False
        self.last_tick = now
        return self.step(force=False)

    def step(self, force=False):
        try:
            if getattr(self.agent.bot, "entity", None) is None:
                return False
            self._update_survival_stuck_state()
            inv = get_item_counts(self.agent)
            action = self._choose_step(inv)
            self.state["last_survival_action"] = action
            self.save()
            add_log(title=self.pack_message("Survival loop step."), content=action, label="plugin")
            self._maybe_update_terrain_snapshot()
            if action == "escape_cave":
                return self._escape_cave_step()
            if action == "escape_stuck":
                return self._escape_stuck_step()
            if action == "build_cliff_path":
                return self._build_cliff_path_step()
            if action == "plant_saplings":
                return self._plant_saplings_step()
            if action == "build_resource_path":
                return self._build_resource_path_step()
            if action == "find_trees":
                return self._find_trees()
            if action == "return_base":
                return self._return_to_base()
            if action == "collect_logs":
                return self._collect_logs()
            if action == "craft_planks":
                return self._craft_planks()
            if action == "craft_table":
                return craft(self.agent, "crafting_table", 1)
            if action == "collect_dirt":
                return collect_blocks(self.agent, "dirt", 8)
            if action == "craft_chest":
                return self._craft_chest()
            if action == "place_storage_chest":
                return self._place_storage_chest()
            if action == "deposit_items":
                return self._deposit_items()
            if action == "find_riverbank":
                return self._find_riverbank()
            if action == "craft_wooden_pickaxe":
                return self._craft_wooden_pickaxe()
            if action == "craft_wooden_tools":
                return self._craft_wooden_tools()
            if action == "collect_stone":
                return self._collect_stone()
            if action == "craft_stone_tools":
                return self._craft_stone_tools()
            if action == "prepare_farm_plot":
                return self._prepare_farm_plot()
            if action == "collect_seeds":
                return self._collect_seeds()
            if action == "build_shelter":
                return self._build_shelter_step()
            if action == "farm":
                return self._farm_step()
            self._report("Survival loop is stocked for now; I will keep checking and improving the base.")
            return True
        except Exception as e:
            add_log(title=self.pack_message("Survival loop failed."), content=str(e), label="warning")
            return False

    def _choose_step(self, inv):
        logs = self._count_logs(inv)
        planks = self._count_planks(inv)
        seeds = inv.get("wheat_seeds", 0)
        cobble = inv.get("cobblestone", 0)
        prepared_tiles = self._prepared_farm_tile_count()
        farm_capacity = len(self._farm_positions())
        minimum_tiles = min(self._farm_initial_target_tiles(), farm_capacity) if farm_capacity > 0 else 0
        farm_failures = int(self.state.get("farm_failures", 0) or 0)
        stone_failures = int(self.state.get("stone_failures", 0) or 0)
        if self._is_survival_stuck():
            return "escape_stuck"
        if logs < 1 and planks < 6 and self._best_log_block() is None:
            self._remember_visible_trees(force=True)
            high_tree = self._nearest_high_remembered_tree()
            if high_tree is not None:
                if self._completed_high_tree_route(high_tree) is not None:
                    return "collect_logs"
                if inv.get("dirt", 0) + inv.get("grass_block", 0) < 8:
                    return "collect_dirt"
                return "build_resource_path"
        if self._should_escape_cave(inv):
            return "escape_cave"
        if self._should_plant_saplings(inv):
            return "plant_saplings"
        if self._needs_basic_wooden_tools(inv):
            if logs < 1 and planks < 6:
                block = self._best_log_block()
                if block is None:
                    self._remember_visible_trees(force=True)
                    high_tree = self._nearest_high_remembered_tree()
                    if high_tree is not None and not self._is_unreachable_resource_target(high_tree):
                        if self._completed_high_tree_route(high_tree) is not None:
                            return "collect_logs"
                        if inv.get("dirt", 0) + inv.get("grass_block", 0) < 8:
                            return "collect_dirt"
                        return "build_resource_path"
                    return "find_trees"
                if self._log_block_requires_resource_path(block):
                    target = {"kind": "tree_high", "x": int(block.position.x), "y": int(block.position.y), "z": int(block.position.z)}
                    if self._is_unreachable_resource_target(target):
                        return "find_trees"
                    if inv.get("dirt", 0) + inv.get("grass_block", 0) < 8:
                        return "collect_dirt"
                    return "build_resource_path"
                return "collect_logs"
            if planks < 8 and logs > 0:
                return "craft_planks"
            if inv.get("crafting_table", 0) < 1 and get_nearest_block(self.agent, "crafting_table", 20) is None and planks >= 4:
                return "craft_table"
            if inv.get("stick", 0) < 4 and planks >= 2:
                return "craft_wooden_tools"
            return "craft_wooden_tools"
        if self._should_return_to_base():
            return "return_base"
        if self.state.get("farm_mode") == "riverbank" and not self._has_riverbank_farm_plan():
            return "find_riverbank"
        if self.state.get("farm_mode") == "riverbank" and self._has_riverbank_farm_plan():
            if prepared_tiles < minimum_tiles:
                return "prepare_farm_plot"
            if self._has_mature_wheat_nearby() or seeds > 0:
                return "farm"
            return "collect_seeds"
        if self._needs_resource_path(inv):
            if inv.get("dirt", 0) + inv.get("grass_block", 0) < 4:
                return "collect_dirt"
            return "build_resource_path"
        if self._needs_tree_search(inv):
            return "find_trees"
        if self.state.get("farm_mode") == "riverbank" and not self._has_riverbank_farm_plan():
            return "find_riverbank"
        if self.state.get("farm_mode") == "riverbank" and self._has_riverbank_farm_plan():
            if prepared_tiles < minimum_tiles:
                return "prepare_farm_plot"
            if self._has_mature_wheat_nearby() or seeds > 0:
                return "farm"
            return "collect_seeds"
        if self._needs_cliff_path():
            if inv.get("dirt", 0) + inv.get("grass_block", 0) < 6:
                return "collect_dirt"
            return "build_cliff_path"
        if farm_failures >= 2:
            self.state["farm_failures"] = 0
            self.state["work_cursor"] = int(self.state.get("work_cursor", 0) or 0) + 1
            self.save()
            fallback = ["prepare_farm_plot", "collect_dirt", "collect_stone", "build_shelter"]
            return fallback[self.state["work_cursor"] % len(fallback)]
        if inv.get("crafting_table", 0) < 1 and get_nearest_block(self.agent, "crafting_table", 20) is None and planks >= 4:
            return "craft_table"
        if not self._storage_chest_exists():
            if int(self.state.get("chest_craft_failures", 0) or 0) >= 2 and not self._farm_plot_ready():
                self.state["chest_craft_failures"] = 0
                self.save()
                return "prepare_farm_plot"
            if inv.get("chest", 0) > 0:
                return "place_storage_chest"
            if planks >= 8:
                return "craft_chest"
            if logs > 0:
                return "craft_planks"
            return "collect_logs"
        if inv.get("wooden_pickaxe", 0) < 1 and inv.get("stone_pickaxe", 0) < 1:
            if logs < 1 and planks < 3:
                return "collect_logs"
            return "craft_wooden_pickaxe"
        if inv.get("wooden_pickaxe", 0) < 1 and inv.get("stone_pickaxe", 0) < 1 and not self.state.get("wooden_pickaxe_crafted", False):
            return "craft_wooden_pickaxe"
        missing_stone_tools = [tool for tool in ["stone_pickaxe", "stone_axe", "stone_shovel", "stone_hoe"] if inv.get(tool, 0) < 1]
        if missing_stone_tools and self._can_craft_any_stone_tool(inv, missing_stone_tools):
            return "craft_stone_tools"
        if self.state.get("farm_mode") == "riverbank" and not self._has_riverbank_farm_plan():
            return "find_riverbank"
        if inv.get("stone_pickaxe", 0) > 0 and inv.get("stone_hoe", 0) > 0 and not self._farm_plot_ready():
            return "prepare_farm_plot"
        if stone_failures >= 3:
            self.state["stone_failures"] = 0
            self.save()
            return "prepare_farm_plot"
        if self.state.get("wooden_pickaxe_crafted", False) and cobble < 6 and (missing_stone_tools or inv.get("stone_pickaxe", 0) < 1):
            return "collect_stone"
        if logs > 0 and planks < 16:
            return "craft_planks"
        if inv.get("stick", 0) < 4 and planks >= 2:
            return "craft_wooden_pickaxe" if not self.state.get("wooden_pickaxe_crafted", False) else "craft_stone_tools"
        if logs < 4 and planks < 8 and cobble >= 14:
            return "collect_logs"
        missing_stone_tools = [tool for tool in ["stone_pickaxe", "stone_axe", "stone_shovel", "stone_hoe"] if inv.get(tool, 0) < 1]
        if missing_stone_tools and self._can_craft_any_stone_tool(inv, missing_stone_tools):
            return "craft_stone_tools"
        if self._should_deposit(inv):
            return "deposit_items"
        if not self._farm_plot_ready():
            return "prepare_farm_plot"
        if inv.get("dirt", 0) + inv.get("grass_block", 0) < 20:
            return "collect_dirt"
        if seeds < 4:
            return "collect_seeds"
        if len(self.state.get("shelter_blocks_done", [])) < len(self._shelter_plan()):
            return "build_shelter"
        return "farm"

    def _should_return_to_base(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return False
        base = self._base()
        dx = pos.x - base["x"]
        dy = pos.y - base["y"]
        dz = pos.z - base["z"]
        max_distance = 56 if self.state.get("farm_mode") == "riverbank" else 24
        return abs(dy) > 12 or math.sqrt(dx * dx + dz * dz) > max_distance

    def _return_to_base(self):
        base = self._base()
        return go_to_position(self.agent, base["x"], base["y"], base["z"], 4)

    def _walkable_ground_names(self):
        return set([
            "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "farmland",
            "stone", "cobblestone", "deepslate", "andesite", "diorite", "granite",
            "sand", "red_sand", "gravel", "clay", "mud",
        ])

    def _soft_route_ground_names(self):
        return set([
            "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "farmland",
            "sand", "red_sand", "gravel", "clay", "mud", "snow", "snow_block",
        ])

    def _is_hard_route_ground_without_tool(self, block_name):
        required = self._required_tool_kind_for_block(block_name)
        return required == "pickaxe" and not self._has_tool_kind("pickaxe")

    def _route_gravity_block_names(self):
        return set(["sand", "red_sand", "gravel"])

    def _route_soft_clearable_names(self):
        return set([
            "short_grass", "tall_grass", "grass", "fern", "large_fern", "dead_bush",
            "snow", "pink_petals", "wildflowers", "leaf_litter",
            "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
            "red_tulip", "orange_tulip", "white_tulip", "pink_tulip",
            "oxeye_daisy", "cornflower", "lily_of_the_valley", "torchflower",
        ])

    def _route_can_break_name(self, x, y, z, block_name):
        if block_name is None or block_name in get_empty_block_names():
            return False
        if block_name in self._liquid_block_names() or block_name in self._farm_danger_blocks():
            return False
        if block_name in self._route_soft_clearable_names():
            return True
        required = self._required_tool_kind_for_block(block_name)
        if required is not None and not self._has_tool_kind(required):
            return False
        for dx, dy, dz in [(0, 1, 0), (-1, 0, 0), (1, 0, 0), (0, 0, -1), (0, 0, 1)]:
            if self._block_name_at(x + dx, y + dy, z + dz) in self._liquid_block_names():
                return False
        if self._block_name_at(x, y + 1, z) in self._route_gravity_block_names():
            return False
        return True

    def _route_clearance_step(self, x, ground_y, z, mode):
        empty = set(get_empty_block_names())
        for clear_y in [ground_y + 1, ground_y + 2]:
            name = self._block_name_at(x, clear_y, z)
            if name is None or name in empty:
                continue
            if self._route_can_break_name(x, clear_y, z, name):
                return [int(x), int(ground_y), int(z), mode, int(clear_y)]
            return None
        return None

    def _route_base_step_cost(self, mode):
        return {
            "start": 0.0,
            "walk": 1.0,
            "jump": 1.25,
            "dig_clear": 4.0,
            "dig_jump_clear": 4.75,
            "walk_hard": 25.0,
            "jump_hard": 30.0,
            "fill_water": 3.0,
            "bridge": 3.5,
            "fill_drop": 2.5,
        }.get(mode, 1.5)

    def _cliff_path_blocks(self):
        return ["dirt", "grass_block"]

    def _liquid_block_names(self):
        return set(["water", "flowing_water", "lava", "flowing_lava"])

    def _cliff_path_key(self, pos):
        return "%d,%d,%d" % (int(pos[0]), int(pos[1]), int(pos[2]))

    def _build_key(self, x, y, z):
        return "%d,%d,%d" % (int(x), int(y), int(z))

    def _is_build_position_blocked(self, x, y, z):
        return self._build_key(x, y, z) in set(self.state.get("blocked_build_positions", []))

    def _mark_build_success(self, x, y, z):
        key = self._build_key(x, y, z)
        changed = False
        failures = dict(self.state.get("build_position_failures", {}) or {})
        if key in failures:
            failures.pop(key, None)
            self.state["build_position_failures"] = failures
            changed = True
        blocked = set(self.state.get("blocked_build_positions", []))
        if key in blocked:
            blocked.remove(key)
            self.state["blocked_build_positions"] = sorted(blocked)
            changed = True
        if changed:
            self.save()

    def _reset_survival_stuck_progress(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is not None:
            self.state["survival_last_position"] = {"x": float(pos.x), "y": float(pos.y), "z": float(pos.z), "t": time.time()}
        self.state["survival_stuck_seconds"] = 0
        self.save()

    def _remember_recent_route_block(self, x, y, z):
        blocks = dict(self.state.get("recent_route_blocks", {}) or {})
        blocks[self._build_key(x, y, z)] = time.time()
        cutoff = time.time() - 120
        self.state["recent_route_blocks"] = {key: value for key, value in blocks.items() if float(value or 0) >= cutoff}
        self._reset_survival_stuck_progress()

    def _is_recent_route_block(self, x, y, z):
        blocks = dict(self.state.get("recent_route_blocks", {}) or {})
        seen = float(blocks.get(self._build_key(x, y, z), 0) or 0)
        return seen > 0 and time.time() - seen <= 120

    def _mark_build_failure(self, x, y, z, reason="placement failed", limit=2):
        key = self._build_key(x, y, z)
        failures = dict(self.state.get("build_position_failures", {}) or {})
        failures[key] = int(failures.get(key, 0) or 0) + 1
        self.state["build_position_failures"] = failures
        if failures[key] >= limit:
            blocked = set(self.state.get("blocked_build_positions", []))
            blocked.add(key)
            self.state["blocked_build_positions"] = sorted(blocked)
            add_log(
                title=self.pack_message("Marked blocked build position."),
                content="%s after %d failures at %s" % (reason, failures[key], key),
                label="warning",
            )
        self.save()

    def _has_build_support(self, x, y, z):
        empty = set(get_empty_block_names())
        bad = empty.union(self._liquid_block_names()).union(self._farm_danger_blocks())
        for dx, dy, dz in [(0, -1, 0), (0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)]:
            name = self._block_name_at(x + dx, y + dy, z + dz)
            if name is not None and name not in bad:
                return True
        return False

    def _can_place_supported(self, x, y, z, reason):
        target = self._block_name_at(x, y, z)
        if target in self._liquid_block_names():
            self._mark_build_failure(x, y, z, "%s target is liquid" % reason, limit=1)
            return False
        if target in self._farm_danger_blocks():
            self._mark_build_failure(x, y, z, "%s target is dangerous" % reason, limit=1)
            return False
        if not self._has_build_support(x, y, z):
            self._mark_build_failure(x, y, z, "%s has no adjacent support" % reason, limit=1)
            return False
        return True

    def _place_route_support_block(self, x, y, z):
        support_y = int(y) - 1
        target = self._block_name_at(x, support_y, z)
        empty = set(get_empty_block_names())
        if target is not None and target not in empty:
            return self._has_build_support(x, y, z)
        if not self._has_build_support(x, support_y, z):
            return False
        block_name = self._best_cliff_path_block()
        try:
            if place_block(self.agent, block_name, x, support_y, z, "bottom", True):
                self._mark_build_success(x, support_y, z)
                self._remember_recent_route_block(x, support_y, z)
                self._report("I placed dirt support so the resource route has an attachment face.")
                return True
        except Exception as e:
            add_log(title=self.pack_message("Resource route support placement failed."), content=str(e), label="warning")
        return False

    def _required_tool_kind_for_block(self, block_name):
        if block_name is None:
            return None
        if block_name.endswith("_log") or block_name.endswith("_stem") or block_name in ["bamboo_block", "stripped_bamboo_block"]:
            return "axe"
        if any(fragment in block_name for fragment in [
            "stone", "granite", "diorite", "andesite", "deepslate", "tuff", "calcite", "basalt",
            "blackstone", "ore", "cobblestone", "netherrack", "obsidian",
        ]):
            return "pickaxe"
        if block_name in [
            "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mud", "clay",
            "sand", "red_sand", "gravel", "snow", "snow_block",
        ]:
            return "shovel"
        if block_name in ["hay_block", "target"] or "leaves" in block_name:
            return None
        return None

    def _has_tool_kind(self, tool_kind):
        if tool_kind is None:
            return True
        inv = get_item_counts(self.agent)
        return any(inv.get(prefix + "_" + tool_kind, 0) > 0 for prefix in ["wooden", "stone", "iron", "golden", "diamond", "netherite"])

    def _emergency_hand_diggable(self, block_name, required_tool):
        if block_name is None:
            return False
        if required_tool == "pickaxe":
            return False
        if required_tool in ["axe", "shovel"]:
            return True
        hand_blocks = set([
            "short_grass", "tall_grass", "grass", "fern", "large_fern", "dead_bush", "snow",
            "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mud", "clay",
            "sand", "red_sand", "gravel", "leaves", "vine", "cobweb",
        ])
        return block_name in hand_blocks or "leaves" in block_name

    def _can_safely_dig_block(self, block, reason, emergency=False):
        if block is None:
            return False
        required = self._required_tool_kind_for_block(block.name)
        if required is not None and not self._has_tool_kind(required):
            if reason == "log collection" and required == "axe":
                add_log(
                    title=self.pack_message("Hand log collection."),
                    content="Collecting %s by hand because logs are needed to make the first tools." % block.name,
                    label="warning",
                )
            if emergency and self._emergency_hand_diggable(block.name, required):
                add_log(
                    title=self.pack_message("Emergency hand digging."),
                    content="Digging %s without %s because PyBot is stuck." % (block.name, required),
                    label="warning",
                )
            elif not (reason == "log collection" and required == "axe"):
                self._mark_build_failure(block.position.x, block.position.y, block.position.z, "%s needs %s for %s" % (reason, required, block.name), limit=1)
                self._report("I found %s in the way, but I need a %s before digging it." % (block.name, required), label="warning")
                return False
        try:
            if hasattr(self.agent.bot, "canDigBlock") and not self.agent.bot.canDigBlock(block):
                self._mark_build_failure(block.position.x, block.position.y, block.position.z, "%s cannot dig %s" % (reason, block.name), limit=1)
                return False
        except Exception:
            pass
        try:
            self.agent.bot.tool.equipForBlock(block)
        except Exception as e:
            if required is not None and not emergency and not (reason == "log collection" and required == "axe"):
                add_log(title=self.pack_message("Tool equip failed."), content="%s while preparing %s for %s" % (e, required, block.name), label="warning")
                return False
        return True

    def _update_survival_stuck_state(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return
        now = time.time()
        current = {"x": float(pos.x), "y": float(pos.y), "z": float(pos.z), "t": now}
        last = self.state.get("survival_last_position")
        if not isinstance(last, dict):
            self.state["survival_last_position"] = current
            self.state["survival_stuck_seconds"] = 0
            self.save()
            return
        dx = current["x"] - float(last.get("x", current["x"]))
        dy = current["y"] - float(last.get("y", current["y"]))
        dz = current["z"] - float(last.get("z", current["z"]))
        moved = math.sqrt(dx * dx + dy * dy + dz * dz)
        elapsed = max(0, now - float(last.get("t", now)))
        active_actions = set([
            "build_cliff_path", "build_resource_path", "find_trees", "return_base", "collect_logs",
            "collect_dirt", "collect_stone", "prepare_farm_plot", "build_shelter", "farm",
        ])
        was_active = self.state.get("last_survival_action") in active_actions
        if moved < 0.55 and was_active:
            self.state["survival_stuck_seconds"] = float(self.state.get("survival_stuck_seconds", 0) or 0) + elapsed
        else:
            self.state["survival_stuck_seconds"] = 0
        self.state["survival_last_position"] = current
        self.save()

    def _is_survival_stuck(self):
        threshold = float(self.agent.configs.get("survival_emergency_stuck_seconds", 18))
        if self.state.get("last_survival_action") == "build_resource_path":
            threshold = max(threshold, 75.0)
            progress_at = float(self.state.get("last_resource_path_progress_at", 0) or 0)
            if time.time() - progress_at < 180:
                threshold = max(threshold, 120.0)
        if self.state.get("last_survival_action") == "collect_logs":
            threshold = max(threshold, 90.0)
        return float(self.state.get("survival_stuck_seconds", 0) or 0) >= threshold

    def _escape_stuck_candidates(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return []
        x, y, z = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
        offsets = []
        for dy in [1, 0, 2]:
            offsets.append((0, dy, 0))
        for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            offsets.append((dx, 1, dz))
            offsets.append((dx, 0, dz))
            offsets.append((dx, 2, dz))
        blocks = []
        empty = set(get_empty_block_names()).union(self._liquid_block_names())
        for dx, dy, dz in offsets:
            block = self.agent.bot.blockAt(vec3.Vec3(x + dx, y + dy, z + dz))
            if block is None or block.name in empty or block.name in self._farm_danger_blocks():
                continue
            if self._is_recent_route_block(block.position.x, block.position.y, block.position.z):
                continue
            required = self._required_tool_kind_for_block(block.name)
            if required is not None and not self._has_tool_kind(required) and not self._emergency_hand_diggable(block.name, required):
                continue
            blocks.append(block)
        return blocks

    def _open_above_count(self, x, y, z, height=8):
        empty = set(get_empty_block_names())
        count = 0
        for yy in range(int(y) + 1, int(y) + height + 1):
            name = self._block_name_at(x, yy, z)
            if name is None or name in empty:
                count += 1
        return count

    def _is_stone_cave_context(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return False
        x, y, z = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
        hard = 0
        open_cells = 0
        empty = set(get_empty_block_names()).union(self._liquid_block_names())
        for dx in [-2, -1, 0, 1, 2]:
            for dz in [-2, -1, 0, 1, 2]:
                for dy in [0, 1, 2]:
                    name = self._block_name_at(x + dx, y + dy, z + dz)
                    if name in empty or name is None:
                        open_cells += 1
                    elif self._is_hard_route_ground_without_tool(name):
                        hard += 1
        if self._open_above_count(x, y, z, 8) >= 7 and hard < 4:
            return False
        return hard >= 3 and open_cells >= 4

    def _should_escape_cave(self, inv):
        if int(self.state.get("cave_escape_failures", 0) or 0) >= 2:
            return False
        action = self.state.get("last_survival_action")
        if self._is_stone_cave_context() and action in ["build_resource_path", "find_trees", "escape_stuck", "collect_logs"]:
            return True
        pos = get_entity_position(self.agent.bot.entity)
        if action in ["find_trees", "escape_stuck"] and self._best_log_block() is None and pos is not None:
            if math.floor(pos.y) >= int(self._base().get("y", 65)) + 8 or float(self.state.get("survival_stuck_seconds", 0) or 0) >= 8:
                return True
        return False

    def _cave_exit_candidates(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return []
        ox, oy, oz = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
        empty = set(get_empty_block_names())
        candidates = []
        for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]:
            best = None
            for distance in range(1, 9):
                x = ox + dx * distance
                z = oz + dz * distance
                for y in [oy, oy + 1, oy - 1, oy + 2]:
                    feet = self._block_name_at(x, y, z)
                    head = self._block_name_at(x, y + 1, z)
                    ground = self._block_name_at(x, y - 1, z)
                    if feet in self._liquid_block_names() or head in self._liquid_block_names():
                        continue
                    if feet not in empty or head not in empty:
                        continue
                    if ground is None or ground in empty or ground in self._liquid_block_names() or ground in self._farm_danger_blocks():
                        continue
                    score = distance * 2 + max(0, y - oy) * 2 + self._open_above_count(x, y, z, 8)
                    for ahead in range(distance + 1, min(distance + 4, 9)):
                        ax = ox + dx * ahead
                        az = oz + dz * ahead
                        for ay in [y, y + 1]:
                            name = self._block_name_at(ax, ay, az)
                            if name in empty or name is None:
                                score += 1
                            elif self._is_hard_route_ground_without_tool(name):
                                score -= 3
                    if best is None or score > best[0]:
                        best = (score, x, y, z, dx, dz)
            if best is not None:
                candidates.append(best)
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates

    def _escape_cave_step(self):
        candidates = self._cave_exit_candidates()
        if not candidates:
            self.state["cave_escape_failures"] = int(self.state.get("cave_escape_failures", 0) or 0) + 1
            self.save()
            add_log(title=self.pack_message("Cave exit not found."), content="No open non-stone step found near the bot.", label="warning")
            return False
        score, x, y, z, dx, dz = candidates[0]
        if self._is_build_position_blocked(x, y, z):
            return False
        if self._resource_path_walkable(x, y - 1, z):
            ok = go_to_position(self.agent, x, y, z, 1)
        else:
            ok = go_to_position(self.agent, x, y, z, 1)
        if ok:
            self.state["survival_stuck_seconds"] = 0
            self.state["last_survival_action"] = None
            self.state["cave_escape_failures"] = 0
            self.save()
            self._report("I am leaving the stone cave first so I can climb the outside wall instead of digging stone.", label="warning")
            return True
        self.state["cave_escape_failures"] = int(self.state.get("cave_escape_failures", 0) or 0) + 1
        self.save()
        self._mark_build_failure(x, y, z, "cave exit movement failed", limit=2)
        return False

    def _escape_pillar_up_target(self, pos):
        if pos is None:
            return None
        x, y, z = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
        target_y = y - 1
        empty = set(get_empty_block_names())
        target = self._block_name_at(x, target_y, z)
        if target is not None and target not in empty:
            return None
        if target in self._liquid_block_names() or target in self._farm_danger_blocks():
            return None
        for clear_y in [y, y + 1]:
            name = self._block_name_at(x, clear_y, z)
            if name is not None and name not in empty:
                return None
        if not self._has_build_support(x, target_y, z):
            return None
        return {"x": x, "y": target_y, "z": z}

    def _escape_pillar_up_step(self):
        inv = get_item_counts(self.agent)
        if inv.get("dirt", 0) + inv.get("grass_block", 0) < 1:
            return False
        block_name = self._best_cliff_path_block()
        start = get_entity_position(self.agent.bot.entity)
        if start is None:
            return False
        try:
            self.agent.bot.setControlState("jump", True)
            time.sleep(0.35)
            jumped = get_entity_position(self.agent.bot.entity)
            target = self._escape_pillar_up_target(jumped)
            if target is None:
                target = self._escape_pillar_up_target(start)
            if target is None:
                return False
            if self._is_build_position_blocked(target["x"], target["y"], target["z"]):
                return False
            if not self._can_place_supported(target["x"], target["y"], target["z"], "emergency pillar-up placement"):
                return False
            support = self.agent.bot.blockAt(vec3.Vec3(target["x"], target["y"] - 1, target["z"]))
            item = get_an_item_in_inventory(self.agent, block_name)
            if support is None or item is None:
                self._mark_build_failure(target["x"], target["y"], target["z"], "emergency pillar-up placement failed")
                return False
            self.agent.bot.equip(item, "hand")
            self.agent.bot.lookAt(support.position.offset(0.5, 1.0, 0.5))
            self.agent.bot.placeBlock(support, vec3.Vec3(0, 1, 0))
            self._mark_build_success(target["x"], target["y"], target["z"])
            self.state["survival_stuck_seconds"] = 0
            self.state["last_survival_action"] = None
            self.save()
            self._report("I jumped and placed a dirt block under myself to climb out of the pit.", label="warning")
            return True
        except Exception as e:
            add_log(title=self.pack_message("Emergency pillar-up failed."), content=str(e), label="warning")
            return False
        finally:
            try:
                self.agent.bot.setControlState("jump", False)
            except Exception:
                pass

    def _escape_stuck_step(self):
        for block in self._escape_stuck_candidates():
            if not self._can_safely_dig_block(block, "emergency unstuck digging", emergency=True):
                continue
            try:
                self.agent.bot.dig(block, timeout=60)
                self.state["survival_stuck_seconds"] = 0
                self.state["last_survival_action"] = None
                self.save()
                self._report("I dug a nearby soft block by hand to escape being stuck.", label="warning")
                return True
            except Exception as e:
                add_log(title=self.pack_message("Emergency unstuck digging failed."), content=str(e), label="warning")
                continue
        if self._escape_pillar_up_step():
            return True
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return False
        try:
            self.agent.bot.setControlState("jump", True)
            time.sleep(0.4)
            self.agent.bot.setControlState("jump", False)
            ok = go_to_position(self.agent, math.floor(pos.x) + 1, math.floor(pos.y), math.floor(pos.z), 1)
            if ok:
                self.state["survival_stuck_seconds"] = 0
                self.state["last_survival_action"] = None
                self.save()
            return ok
        finally:
            try:
                self.agent.bot.setControlState("jump", False)
            except Exception:
                pass

    def _surface_y_at(self, x, z, center_y, radius=10):
        empty = set(get_empty_block_names())
        dangers = self._farm_danger_blocks().union(set(["water", "lava", "flowing_water", "flowing_lava"]))
        for y in range(int(center_y) + radius, int(center_y) - radius - 1, -1):
            ground = self._block_name_at(x, y, z)
            head = self._block_name_at(x, y + 1, z)
            if ground is None or ground in empty or ground in dangers:
                continue
            if head is None or head in empty:
                return y
        return int(center_y)

    def _cliff_path_anchor_points(self):
        base = self._base()
        current = get_entity_position(self.agent.bot.entity)
        target = self._riverbank_search_target()
        saved_farm = self.state.get("riverbank_farm_positions")
        if isinstance(saved_farm, list) and saved_farm:
            xs = [int(pos[0]) for pos in saved_farm if isinstance(pos, list) and len(pos) >= 3]
            ys = [int(pos[1]) for pos in saved_farm if isinstance(pos, list) and len(pos) >= 3]
            zs = [int(pos[2]) for pos in saved_farm if isinstance(pos, list) and len(pos) >= 3]
            if xs and ys and zs:
                target = {"x": sum(xs) // len(xs), "y": sum(ys) // len(ys) + 1, "z": sum(zs) // len(zs)}
        anchors = []
        if current is not None:
            anchors.append((
                {"x": math.floor(current.x), "y": math.floor(current.y) - 1, "z": math.floor(current.z)},
                base,
            ))
        anchors.append((base, target))
        return anchors

    def _plan_cliff_path_between(self, start, target):
        sx, sz = int(start["x"]), int(start["z"])
        tx, tz = int(target["x"]), int(target["z"])
        sy = self._surface_y_at(sx, sz, int(start["y"]))
        ty = self._surface_y_at(tx, tz, int(target["y"]))
        if abs(ty - sy) < 4:
            return []
        if ty >= sy:
            low = {"x": sx, "y": sy, "z": sz}
            high = {"x": tx, "y": ty, "z": tz}
        else:
            low = {"x": tx, "y": ty, "z": tz}
            high = {"x": sx, "y": sy, "z": sz}
        dx = high["x"] - low["x"]
        dz = high["z"] - low["z"]
        dy = high["y"] - low["y"]
        horizontal = max(abs(dx), abs(dz), 1)
        steps = max(horizontal, dy + 2)
        plan = []
        seen = set()
        for i in range(steps + 1):
            t = float(i) / float(steps)
            x = int(round(low["x"] + dx * t))
            z = int(round(low["z"] + dz * t))
            y = int(low["y"] + math.floor(dy * t))
            key = self._cliff_path_key((x, y, z))
            if key in seen:
                continue
            seen.add(key)
            plan.append([x, y, z])
        return plan

    def _cliff_path_plan(self):
        saved = self.state.get("cliff_path_plan")
        if isinstance(saved, list) and saved:
            return [list(pos) for pos in saved if isinstance(pos, list) and len(pos) >= 3]
        best = []
        for start, target in self._cliff_path_anchor_points():
            plan = self._plan_cliff_path_between(start, target)
            if len(plan) > len(best):
                best = plan
        if best:
            self.state["cliff_path_plan"] = best
            self.state["cliff_path_done"] = []
            self.save()
        return best

    def _needs_cliff_path(self):
        if self._cliff_path_plan():
            done = set(self.state.get("cliff_path_done", []))
            return len(done) < len(self._cliff_path_plan())
        return False

    def _best_cliff_path_block(self):
        inv = get_item_counts(self.agent)
        for name in self._cliff_path_blocks():
            if inv.get(name, 0) > 0:
                return name
        return "dirt"

    def _clear_cliff_path_headroom(self, x, y, z):
        empty = set(get_empty_block_names())
        for clear_y in [y + 1, y + 2]:
            block = self.agent.bot.blockAt(vec3.Vec3(x, clear_y, z))
            if block is None or block.name in empty:
                continue
            if not self._is_farm_clearable(block.name):
                return False
            if not self._can_safely_dig_block(block, "cliff path headroom clearing"):
                return False
            try:
                go_to_position(self.agent, x, y + 1, z, 4)
                self.agent.bot.dig(block, timeout=45)
                self._report("I cleared headroom for the cliff path.")
                return True
            except Exception as e:
                add_log(title=self.pack_message("Cliff path headroom clearing failed."), content=str(e), label="warning")
                return False
        return True

    def _build_cliff_path_step(self):
        plan = self._cliff_path_plan()
        if not plan:
            self._report("I do not see a cliff route that needs a dirt stair path right now.")
            return False
        inv = get_item_counts(self.agent)
        if inv.get("dirt", 0) + inv.get("grass_block", 0) < 1:
            return collect_blocks(self.agent, "dirt", 8)
        done = set(self.state.get("cliff_path_done", []))
        empty = set(get_empty_block_names())
        dangers = self._farm_danger_blocks().union(set(["water", "lava", "flowing_water", "flowing_lava"]))
        for x, y, z in plan:
            key = self._cliff_path_key((x, y, z))
            if key in done or self._is_build_position_blocked(x, y, z):
                continue
            ground = self.agent.bot.blockAt(vec3.Vec3(x, y, z))
            ground_name = None if ground is None else ground.name
            if ground_name in self._liquid_block_names():
                self._mark_build_failure(x, y, z, "cliff path target is liquid", limit=1)
                continue
            if ground_name in dangers:
                add_log(title=self.pack_message("Cliff path blocked by danger."), content="%s at (%d, %d, %d)" % (ground_name, x, y, z), label="warning")
                return False
            if ground_name is None or ground_name in empty:
                if not self._has_build_support(x, y, z):
                    self._mark_build_failure(x, y, z, "cliff path has no adjacent support", limit=1)
                    continue
                block_name = self._best_cliff_path_block()
                if place_block(self.agent, block_name, x, y, z, "bottom", True):
                    self._mark_build_success(x, y, z)
                    self._report("I placed dirt to extend the cliff stair path.")
                    return True
                self._mark_build_failure(x, y, z, "cliff path dirt placement failed")
                return False
            if ground_name not in self._walkable_ground_names():
                if not self._is_farm_clearable(ground_name):
                    add_log(title=self.pack_message("Cliff path blocked."), content="%s at (%d, %d, %d)" % (ground_name, x, y, z), label="warning")
                    return False
                if not self._can_safely_dig_block(ground, "cliff path ground clearing"):
                    return False
                try:
                    go_to_position(self.agent, x, y + 1, z, 4)
                    self.agent.bot.dig(ground, timeout=45)
                    self._report("I cleared weak ground so I can build a stable dirt stair path.")
                    return True
                except Exception as e:
                    add_log(title=self.pack_message("Cliff path ground clearing failed."), content=str(e), label="warning")
                    return False
            if not self._clear_cliff_path_headroom(x, y, z):
                return False
            done.add(key)
            self.state["cliff_path_done"] = sorted(done)
            self.save()
            self._mark_build_success(x, y, z)
            self._report("I marked one cliff path step as walkable.")
            return True
        self._report("The dirt stair path across the cliff is ready for hauling resources.")
        return True

    def _resource_path_target(self, inv):
        logs = self._count_logs(inv)
        planks = self._count_planks(inv)
        self._remember_visible_trees()
        if self._needs_basic_wooden_tools(inv) and logs < 1 and planks < 6:
            remembered = self._nearest_remembered_tree()
            if remembered is not None and not self._is_unreachable_resource_target(remembered):
                return self._normalize_tree_route_target(remembered)
            high_tree = self._nearest_high_remembered_tree()
            if high_tree is not None and not self._is_unreachable_resource_target(high_tree):
                return high_tree
            block = self._best_log_block()
            if block is not None:
                kind = "tree_high" if self._log_block_requires_resource_path(block) else "tree"
                target = {"kind": kind, "x": int(block.position.x), "y": int(block.position.y), "z": int(block.position.z)}
                if not self._is_unreachable_resource_target(target):
                    return target
            return self._tree_search_target()
        if self.state.get("farm_mode") == "riverbank" and not self._has_riverbank_farm_plan():
            target = self._riverbank_search_target()
            water = get_nearest_block(self.agent, "water", 64)
            if water is not None:
                return {"kind": "water", "x": int(water.position.x), "y": int(water.position.y), "z": int(water.position.z)}
            return {"kind": "water", "x": target["x"], "y": target["y"], "z": target["z"]}
        return None

    def _resource_path_key_for(self, target):
        return "resource_path_%s_%d_%d" % (target["kind"], int(target["x"]), int(target["z"]))

    def _resource_route_scan_radius(self):
        return int(self.agent.configs.get("resource_route_scan_radius", 48))

    def _resource_tree_scan_radius(self):
        return int(self.agent.configs.get("resource_tree_scan_radius", 64))

    def _resource_tree_scan_interval_seconds(self):
        return float(self.agent.configs.get("resource_tree_scan_interval_seconds", 180))

    def _remember_visible_trees(self, force=False):
        known = self.state.get("known_resource_trees", [])
        now = time.time()
        last = float(self.state.get("resource_tree_scan_at", 0) or 0)
        if not force and isinstance(known, list) and known and now - last < self._resource_tree_scan_interval_seconds():
            return known
        if not force and isinstance(known, list) and known and last == 0:
            return known
        log_names = [wood + "_log" for wood in get_wood_types()] + ["crimson_stem", "warped_stem", "bamboo_block"]
        blocks = get_nearest_blocks(self.agent, log_names, self._resource_tree_scan_radius(), 12)
        if not blocks:
            self.state["resource_tree_scan_at"] = now
            self.save()
            return []
        remembered = {}
        for item in self.state.get("known_resource_trees", []):
            if not isinstance(item, dict):
                continue
            key = "%d,%d,%d" % (int(item.get("x", 0)), int(item.get("y", 0)), int(item.get("z", 0)))
            remembered[key] = item
        for block in blocks:
            x, y, z = int(block.position.x), int(block.position.y), int(block.position.z)
            key = "%d,%d,%d" % (x, y, z)
            remembered[key] = {"kind": "tree", "x": x, "y": y, "z": z, "name": block.name, "seen_at": int(now)}
        trees = sorted(
            remembered.values(),
            key=lambda item: int(item.get("seen_at", 0) or 0),
            reverse=True,
        )[:64]
        self.state["known_resource_trees"] = trees
        self.state["resource_tree_scan_at"] = now
        self.save()
        return trees

    def _nearest_remembered_tree(self):
        trees = self._remember_visible_trees()
        if not trees:
            trees = self.state.get("known_resource_trees", [])
        pos = get_entity_position(self.agent.bot.entity)
        base = self._base()
        ox = math.floor(pos.x) if pos is not None else int(base["x"])
        oz = math.floor(pos.z) if pos is not None else int(base["z"])
        valid = []
        for item in trees:
            if not isinstance(item, dict):
                continue
            x, y, z = int(item.get("x", 0)), int(item.get("y", base["y"])), int(item.get("z", 0))
            if abs(y - int(base["y"])) > 16 and pos is None:
                continue
            if pos is not None and abs(y - math.floor(pos.y)) > 12:
                continue
            name = self._block_name_at(x, y, z)
            if name is not None and (name.endswith("_log") or name.endswith("_stem") or name == "bamboo_block"):
                valid.append({"kind": "tree", "x": x, "y": y, "z": z, "name": name})
        if not valid:
            return None
        valid.sort(key=lambda item: abs(int(item["x"]) - ox) + abs(int(item["z"]) - oz))
        return valid[0]

    def _nearest_high_remembered_tree(self):
        trees = self.state.get("known_resource_trees", [])
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None or not isinstance(trees, list):
            return None
        ox, oy, oz = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
        candidates = []
        for item in trees:
            if not isinstance(item, dict):
                continue
            x, y, z = int(item.get("x", 0)), int(item.get("y", 0)), int(item.get("z", 0))
            name = item.get("name", "")
            if not (name.endswith("_log") or name.endswith("_stem") or name == "bamboo_block"):
                continue
            if y - oy < 9:
                continue
            if abs(x - ox) + abs(z - oz) > self._resource_route_scan_radius():
                continue
            if self._high_tree_failure_count(x, z) >= 5:
                continue
            access = self._high_tree_access_target(x, y, z, name)
            if access is not None:
                if self._is_unreachable_resource_target(access):
                    continue
                score = abs(access["x"] - ox) + abs(access["z"] - oz) + max(0, access["y"] - oy)
                candidates.append((score, access))
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1] if candidates else None

    def _high_tree_failure_count(self, tree_x, tree_z):
        failures = dict(self.state.get("resource_path_plan_failures", {}) or {})
        total = 0
        for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            total += int(failures.get("tree_high:%d,%d" % (int(tree_x) + dx, int(tree_z) + dz), 0) or 0)
        return total

    def _high_tree_total_failure_count(self):
        failures = dict(self.state.get("resource_path_plan_failures", {}) or {})
        return sum(int(value or 0) for key, value in failures.items() if str(key).startswith("tree_high:"))

    def _drop_failed_high_tree_routes(self):
        if self._high_tree_total_failure_count() < 2:
            return
        changed = False
        for key in list(self.state.keys()):
            if str(key).startswith("resource_path_tree_high_"):
                self.state.pop(key, None)
                changed = True
        if changed:
            add_log(title=self.pack_message("Dropped failed high-tree routes."), content="High-tree routes are paused after repeated failures.", label="warning")
            self.save()

    def _high_tree_access_target(self, x, y, z, name="tree"):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return None
        ox, oz = math.floor(pos.x), math.floor(pos.z)
        options = []
        for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            sx, sz = x + dx, z + dz
            options.append((abs(sx - ox) + abs(sz - oz), sx, sz))
        options.sort(key=lambda item: item[0])
        for _, sx, sz in options:
            access = {
                "kind": "tree_high",
                "x": sx,
                "y": max(int(y) - 1, math.floor(pos.y)),
                "z": sz,
                "tree_x": int(x),
                "tree_y": int(y),
                "tree_z": int(z),
                "name": name,
            }
            if not self._is_unreachable_resource_target(access):
                return access
        return None

    def _route_start_point(self):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is not None:
            return {"x": math.floor(pos.x), "y": math.floor(pos.y) - 1, "z": math.floor(pos.z)}
        return self._base()

    def _route_start_surface_y(self, start, vertical_radius=12):
        actual_y = int(start["y"])
        surface_y = self._surface_y_at(start["x"], start["z"], actual_y, vertical_radius)
        if int(surface_y) > actual_y + 1:
            return actual_y
        if int(surface_y) < actual_y - 4:
            return actual_y
        return int(surface_y)

    def _resource_route_matches_current_start(self, route):
        if not route:
            return False
        start = self._route_start_point()
        expected_y = self._route_start_surface_y(start, 12)
        first = route[0]
        dx = abs(int(first[0]) - int(start["x"]))
        dy = abs(int(first[1]) - int(expected_y))
        dz = abs(int(first[2]) - int(start["z"]))
        return dx + dz <= 4 and dy <= 2

    def _route_step_from_column(self, col, current_y):
        if col is None or col.get("danger"):
            return None
        options = []
        sy = col.get("surface_y")
        if col.get("walkable") and sy is not None and abs(int(sy) - int(current_y)) <= 1:
            if self._is_hard_route_ground_without_tool(col.get("surface")):
                build_y = int(sy) + 1
                if abs(build_y - int(current_y)) <= 1 and self._has_build_support(int(col["x"]), build_y, int(col["z"])):
                    options.append([int(col["x"]), build_y, int(col["z"]), "bridge"])
            else:
                mode = "jump" if int(sy) > int(current_y) else "walk"
                options.append([int(col["x"]), int(sy), int(col["z"]), mode])
                clear = self._route_clearance_step(int(col["x"]), int(sy), int(col["z"]), "dig_jump_clear" if mode == "jump" else "dig_clear")
                if clear is not None:
                    options.append(clear)
        for ground_y in [int(current_y), int(current_y) + 1]:
            if sy is not None and int(sy) == ground_y:
                continue
            ground_name = self._block_name_at(int(col["x"]), ground_y, int(col["z"]))
            if ground_name in self._walkable_ground_names() and not self._is_hard_route_ground_without_tool(ground_name):
                clear = self._route_clearance_step(
                    int(col["x"]),
                    ground_y,
                    int(col["z"]),
                    "dig_jump_clear" if ground_y > int(current_y) else "dig_clear",
                )
                if clear is not None and abs(ground_y - int(current_y)) <= 1:
                    options.append(clear)
        if col.get("water_depth") == 1 and col.get("water_y") is not None:
            wy = int(col["water_y"])
            if abs(wy - int(current_y)) <= 1:
                options.append([int(col["x"]), wy, int(col["z"]), "fill_water"])
        build_y = int(current_y)
        if sy is None and self._has_build_support(int(col["x"]), build_y, int(col["z"])):
            options.append([int(col["x"]), build_y, int(col["z"]), "bridge"])
        if sy is not None and int(sy) < int(current_y) - 1 and self._has_build_support(int(col["x"]), int(current_y) - 1, int(col["z"])):
            options.append([int(col["x"]), int(current_y) - 1, int(col["z"]), "fill_drop"])
        if not options:
            return None
        options.sort(key=self._route_step_cost)
        return options[0]

    def _route_weight_key(self, x, y, z):
        return "%d,%d,%d" % (int(x), int(y), int(z))

    def _resource_route_weights(self):
        weights = self.state.get("resource_route_weights", {})
        return weights if isinstance(weights, dict) else {}

    def _route_memory_bonus(self, x, y, z):
        weight = float(self._resource_route_weights().get(self._route_weight_key(x, y, z), 0) or 0)
        return min(0.85, weight / 40.0)

    def _route_step_cost(self, step):
        mode = step[3] if len(step) > 3 else "walk"
        base = self._route_base_step_cost(mode)
        return max(0.2, base - self._route_memory_bonus(step[0], step[1], step[2]))

    def _remember_successful_resource_route(self, target, plan, reached=False):
        if not plan:
            return
        weights = dict(self._resource_route_weights())
        route_distance = 0
        previous = None
        for step in plan:
            if previous is not None:
                route_distance += abs(int(step[0]) - int(previous[0])) + abs(int(step[2]) - int(previous[2]))
            previous = step
        distance_bonus = max(1, min(12, int(route_distance / 8) + 1))
        tx, tz = int(target["x"]), int(target["z"])
        for index, step in enumerate(plan):
            x, y, z = int(step[0]), int(step[1]), int(step[2])
            distance_to_target = abs(x - tx) + abs(z - tz)
            target_bonus = max(1, min(8, int((route_distance - distance_to_target) / 8) + 1))
            mode_bonus = 2 if len(step) > 3 and step[3] in ["fill_water", "bridge", "fill_drop"] else 1
            key = self._route_weight_key(x, y, z)
            weights[key] = min(999, int(weights.get(key, 0) or 0) + distance_bonus + target_bonus + mode_bonus)
        self.state["resource_route_weights"] = weights
        saved_routes = self.state.get("successful_resource_routes", {})
        routes = dict(saved_routes) if isinstance(saved_routes, dict) else {}
        route_key = "%s:%d,%d" % (target.get("kind", "resource"), int(target["x"]), int(target["z"]))
        record = routes.get(route_key, {})
        record["successes"] = int(record.get("successes", 0) or 0) + 1
        record["last_distance"] = route_distance
        record["last_reached"] = bool(reached)
        record["last_seen_at"] = int(time.time())
        record["weight"] = int(record.get("weight", 0) or 0) + distance_bonus + (8 if reached else 0)
        routes[route_key] = record
        self.state["successful_resource_routes"] = routes
        self.save()

    def _plan_resource_route(self, target):
        if target.get("kind") == "tree_high":
            return self._plan_high_tree_ramp(target)
        start = self._route_start_point()
        sx, sy, sz = int(start["x"]), self._route_start_surface_y(start, 12), int(start["z"])
        tx, tz = int(target["x"]), int(target["z"])
        radius = max(abs(tx - sx), abs(tz - sz)) + 8
        radius = min(max(radius, 12), self._resource_route_scan_radius())
        min_x, max_x = sx - radius, sx + radius
        min_z, max_z = sz - radius, sz + radius
        if not (min_x <= tx <= max_x and min_z <= tz <= max_z):
            scale = float(radius - 2) / float(max(abs(tx - sx), abs(tz - sz), 1))
            tx = int(round(sx + (tx - sx) * scale))
            tz = int(round(sz + (tz - sz) * scale))
            radius = max(abs(tx - sx), abs(tz - sz)) + 8
        if target.get("kind") in ["tree_search", "tree_access_step"]:
            radius = min(radius, 12)
        min_x, max_x = sx - radius, sx + radius
        min_z, max_z = sz - radius, sz + radius
        columns = {}
        for x in range(min_x, max_x + 1):
            for z in range(min_z, max_z + 1):
                columns[(x, z)] = self._terrain_column(x, z, sy, vertical_radius=12)
        start_node = (sx, sz)
        counter = 0
        queue = [(0.0, counter, start_node)]
        parents = {start_node: None}
        steps = {start_node: [sx, sy, sz, "start"]}
        costs = {start_node: 0.0}
        best = start_node
        best_dist = abs(sx - tx) + abs(sz - tz)
        while queue and len(parents) < 1800:
            _, _, node = heapq.heappop(queue)
            x, z = node
            step = steps[node]
            dist = abs(x - tx) + abs(z - tz)
            if dist < best_dist:
                best = node
                best_dist = dist
            if dist <= 2:
                best = node
                break
            for ox, oz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nxt = (x + ox, z + oz)
                if nxt not in columns:
                    continue
                route_step = self._route_step_from_column(columns[nxt], step[1])
                if route_step is None:
                    continue
                new_cost = costs[node] + self._route_step_cost(route_step)
                if nxt in costs and new_cost >= costs[nxt]:
                    continue
                parents[nxt] = node
                steps[nxt] = route_step
                costs[nxt] = new_cost
                counter += 1
                priority = new_cost + (abs(nxt[0] - tx) + abs(nxt[1] - tz)) * 0.15
                heapq.heappush(queue, (priority, counter, nxt))
        if best == start_node and best_dist > 2:
            return []
        path = []
        node = best
        while node is not None:
            path.append(steps[node])
            node = parents[node]
        path.reverse()
        return path

    def _plan_high_tree_ramp(self, target):
        start = self._route_start_point()
        sx = int(start["x"])
        sz = int(start["z"])
        sy = self._route_start_surface_y(start, 12)
        tx = int(target["x"])
        tz = int(target["z"])
        ty = int(target["y"])
        dx = tx - sx
        dz = tz - sz
        horizontal_total = max(abs(dx), abs(dz), 1)
        if horizontal_total > 14:
            segment = min(12, horizontal_total)
            scale = float(segment) / float(horizontal_total)
            tx = int(round(sx + dx * scale))
            tz = int(round(sz + dz * scale))
            ty = sy + min(segment, max(0, ty - sy))
            dx = tx - sx
            dz = tz - sz
        plan = []
        dy = max(0, ty - sy)
        horizontal = max(abs(dx) + abs(dz), 1)
        steps = max(horizontal, dy + 2)
        if steps > max(12, self._resource_route_scan_radius()):
            steps = max(12, self._resource_route_scan_radius())
        seen = set()
        for step in plan:
            if len(step) >= 3:
                seen.add(self._build_key(step[0], step[1], step[2]))
        def add_ramp_step(px, py, pz, force_bridge=False):
            key = self._build_key(px, py, pz)
            if key in seen:
                return True
            if self._is_build_position_blocked(px, py, pz):
                return False
            seen.add(key)
            mode = "start" if not plan else ("jump" if py > plan[-1][1] else "walk")
            ground = self._block_name_at(px, py, pz)
            head = self._block_name_at(px, py + 1, pz)
            empty = set(get_empty_block_names())
            if not force_bridge and ground in self._walkable_ground_names() and (head is None or head in empty):
                if self._is_hard_route_ground_without_tool(ground):
                    mode = "jump_hard" if mode == "jump" else "walk_hard"
                plan.append([px, py, pz, mode])
            else:
                plan.append([px, py, pz, "bridge"])
            return True
        x, z, y = sx, sz, sy
        x_dir = 1 if tx > sx else -1
        z_dir = 1 if tz > sz else -1
        for i in range(steps + 1):
            previous_y = y
            if i > 0:
                if abs(tx - x) >= abs(tz - z) and x != tx:
                    x += x_dir
                elif z != tz:
                    z += z_dir
                elif x != tx:
                    x += x_dir
                if y < ty:
                    y += 1
            if y > previous_y and not add_ramp_step(x, previous_y, z, force_bridge=True):
                break
            if not add_ramp_step(x, y, z):
                break
        return plan

    def _resource_path_plan(self, target):
        state_key = self._resource_path_key_for(target)
        saved = self.state.get(state_key)
        if isinstance(saved, list) and saved:
            route = [list(pos) for pos in saved if isinstance(pos, list) and len(pos) >= 3]
            blocked = [pos for pos in route if len(pos) >= 3 and self._is_build_position_blocked(pos[0], pos[1], pos[2])]
            if blocked:
                self._invalidate_resource_path(target, "saved route contains blocked build positions")
            elif route and all(len(pos) >= 4 for pos in route) and self._resource_route_matches_current_start(route):
                return route
            else:
                self.state.pop(state_key, None)
                self.state.pop(state_key + "_done", None)
        plan = self._plan_resource_route(target)
        if not plan:
            self._mark_unreachable_resource_target(target)
            return []
        if target.get("kind") == "tree_high" and len(plan) < 2:
            self._mark_unreachable_resource_target(target)
            add_log(title=self.pack_message("Resource route unreachable."), content="tree_high plan has no climb step", label="warning")
            return []
        self.state[state_key] = plan
        self.state[state_key + "_done"] = []
        self.save()
        return plan

    def _tree_high_route_reached_target(self, target, plan):
        if target.get("kind") != "tree_high" or not plan:
            return True
        top = plan[-1]
        if len(top) < 3:
            return False
        horizontal = abs(int(top[0]) - int(target["x"])) + abs(int(top[2]) - int(target["z"]))
        vertical = int(target["y"]) - int(top[1])
        return horizontal <= 3 and vertical <= 2

    def _advance_incomplete_tree_high_segment(self, target, plan):
        if self._tree_high_route_reached_target(target, plan):
            return False
        state_key = self._resource_path_key_for(target)
        self.state.pop(state_key, None)
        self.state.pop(state_key + "_done", None)
        self.save()
        add_log(
            title=self.pack_message("Resource route segment complete."),
            content="%s needs another segment toward the high tree." % state_key,
            label="plugin",
        )
        return True

    def _invalidate_resource_path(self, target, reason):
        state_key = self._resource_path_key_for(target)
        self.state.pop(state_key, None)
        self.state.pop(state_key + "_done", None)
        failures = dict(self.state.get("resource_path_plan_failures", {}) or {})
        route_key = "%s:%d,%d" % (target.get("kind", "resource"), int(target["x"]), int(target["z"]))
        failures[route_key] = int(failures.get(route_key, 0) or 0) + 1
        self.state["resource_path_plan_failures"] = failures
        self.save()
        add_log(title=self.pack_message("Resource route invalidated."), content="%s: %s" % (route_key, reason), label="warning")

    def _resource_path_walkable(self, x, y, z):
        ground = self._block_name_at(x, y, z)
        head = self._block_name_at(x, y + 1, z)
        if ground not in self._walkable_ground_names():
            return False
        return head is None or head in get_empty_block_names()

    def _needs_resource_path(self, inv):
        target = self._resource_path_target(inv)
        if target is None:
            return False
        plan = self._resource_path_plan(target)
        if not plan:
            return False
        done_key = self._resource_path_key_for(target) + "_done"
        done = set(self.state.get(done_key, []))
        if len(done) >= len(plan) and target.get("kind") == "tree_high":
            if self._advance_incomplete_tree_high_segment(target, plan):
                return True
        return len(done) < min(len(plan), 18)

    def _normalize_tree_route_target(self, target):
        if target is None or target.get("kind") != "tree":
            return target
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return target
        dx = abs(int(target["x"]) - math.floor(pos.x))
        dy = int(target["y"]) - math.floor(pos.y)
        dz = abs(int(target["z"]) - math.floor(pos.z))
        if dy > 2 or dx + dz > 3:
            normalized = dict(target)
            normalized["kind"] = "tree_high"
            return normalized
        return target

    def _completed_high_tree_route(self, target=None):
        if target is not None:
            key = self._resource_path_key_for(target)
            plan = self.state.get(key)
            if not isinstance(plan, list) or not plan:
                return None
            done = set(self.state.get(key + "_done", []) or [])
            if len(done) < len(plan):
                return None
            return {"key": key, "plan": plan, "top": plan[-1]}
        pos = get_entity_position(self.agent.bot.entity)
        routes = []
        for key, plan in self.state.items():
            if not key.startswith("resource_path_tree_high_") or key.endswith("_done"):
                continue
            if not isinstance(plan, list) or not plan:
                continue
            done = set(self.state.get(key + "_done", []) or [])
            if len(done) < len(plan):
                continue
            top = plan[-1]
            if len(top) < 3:
                continue
            distance = 0
            if pos is not None:
                distance = abs(math.floor(pos.x) - int(top[0])) + abs(math.floor(pos.y) - (int(top[1]) + 1)) + abs(math.floor(pos.z) - int(top[2]))
            routes.append((distance, key, plan))
        routes.sort(key=lambda item: item[0])
        if not routes:
            return None
        return {"key": routes[0][1], "plan": routes[0][2], "top": routes[0][2][-1]}

    def _move_to_completed_high_tree_route(self):
        route = self._completed_high_tree_route()
        if route is None:
            return False
        x, y, z, mode = self._resource_route_step_parts(route["top"])
        pos = get_entity_position(self.agent.bot.entity)
        if pos is not None:
            distance = abs(math.floor(pos.x) - x) + abs(math.floor(pos.y) - (y + 1)) + abs(math.floor(pos.z) - z)
            if distance <= 3:
                return False
        return self._move_to_resource_step(x, y, z, mode, distance=2)

    def _mark_unreachable_resource_target(self, target):
        key = "%s:%d,%d" % (target.get("kind", "resource"), int(target["x"]), int(target["z"]))
        blocked = set(self.state.get("unreachable_resource_targets", []))
        blocked.add(key)
        self.state["unreachable_resource_targets"] = sorted(blocked)
        self.save()
        add_log(title=self.pack_message("Resource route unreachable."), content=key, label="warning")

    def _is_unreachable_resource_target(self, target):
        key = "%s:%d,%d" % (target.get("kind", "resource"), int(target["x"]), int(target["z"]))
        return key in set(self.state.get("unreachable_resource_targets", []))

    def _resource_route_step_parts(self, step):
        x, y, z = int(step[0]), int(step[1]), int(step[2])
        mode = step[3] if len(step) > 3 else "walk"
        return x, y, z, mode

    def _move_to_resource_step(self, x, y, z, mode="walk", distance=1):
        pos = get_entity_position(self.agent.bot.entity)
        jump = mode in ["jump", "jump_hard"] or (pos is not None and int(y) >= math.floor(pos.y))
        try:
            if jump:
                self.agent.bot.setControlState("jump", True)
                time.sleep(0.2)
            return go_to_position(self.agent, x, y + 1, z, distance)
        finally:
            try:
                self.agent.bot.setControlState("jump", False)
            except Exception:
                pass

    def _place_resource_route_block(self, x, y, z, mode):
        if mode in ["fill_water", "bridge", "fill_drop"] or not self._resource_path_walkable(x, y, z):
            target = self._block_name_at(x, y, z)
            empty = set(get_empty_block_names())
            if target is not None and target not in empty and target not in self._liquid_block_names():
                if target in self._walkable_ground_names():
                    self._mark_build_success(x, y, z)
                    self._remember_recent_route_block(x, y, z)
                    return True
                if self._route_can_break_name(x, y, z, target):
                    block = self.agent.bot.blockAt(vec3.Vec3(x, y, z))
                    try:
                        if block is not None:
                            self.agent.bot.dig(block, timeout=30)
                            time.sleep(0.3)
                            target = self._block_name_at(x, y, z)
                            if target is None or target in empty:
                                return self._place_resource_route_block(x, y, z, mode)
                    except Exception as e:
                        self._mark_build_failure(x, y, z, "resource route soft clearing failed")
                        add_log(title=self.pack_message("Resource route soft clearing failed."), content=str(e), label="warning")
                        return False
                self._mark_build_failure(x, y, z, "resource route target occupied by %s" % target, limit=1)
                return False
            if not self._has_build_support(x, y, z) and not self._place_route_support_block(x, y, z):
                self._mark_build_failure(x, y, z, "resource route block placement has no adjacent support", limit=1)
                return False
            if not self._can_place_supported(x, y, z, "resource route block placement"):
                return False
            block_name = self._best_cliff_path_block()
            try:
                if place_block(self.agent, block_name, x, y, z, "bottom", True):
                    self._mark_build_success(x, y, z)
                    self._remember_recent_route_block(x, y, z)
                    self._report("I placed dirt to make the resource route walkable.")
                    return True
            except Exception as e:
                self._mark_build_failure(x, y, z, "resource route block placement raised %s" % e, limit=1)
                add_log(title=self.pack_message("Resource route placement failed."), content=str(e), label="warning")
                return False
            self._mark_build_failure(x, y, z, "resource route block placement failed")
            return False
        return True

    def _move_to_route_frontier(self, plan, done):
        if not plan or not done:
            return False
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return False
        frontier = None
        frontier_index = -1
        for index, step in enumerate(plan):
            if len(step) < 3:
                continue
            if self._build_key(step[0], step[1], step[2]) in done:
                frontier = step
                frontier_index = index
        if frontier is None or frontier_index >= len(plan) - 1:
            return False
        x, y, z, mode = self._resource_route_step_parts(frontier)
        distance = abs(math.floor(pos.x) - x) + abs(math.floor(pos.y) - (y + 1)) + abs(math.floor(pos.z) - z)
        if distance <= 4:
            return False
        return self._move_to_resource_step(x, y, z, mode, distance=1)

    def _is_current_route_air_step(self, x, y, z, mode):
        if mode not in ["bridge", "jump"]:
            return False
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return False
        if math.floor(pos.x) != int(x) or math.floor(pos.z) != int(z) or math.floor(pos.y) != int(y):
            return False
        current = self._block_name_at(x, y, z)
        below = self._block_name_at(x, y - 1, z)
        empty = set(get_empty_block_names())
        if current is not None and current not in empty:
            return False
        return below in self._walkable_ground_names()

    def _build_resource_path_step(self):
        inv = get_item_counts(self.agent)
        target = self._resource_path_target(inv)
        if target is None:
            return False
        if inv.get("dirt", 0) + inv.get("grass_block", 0) < 1:
            return collect_blocks(self.agent, "dirt", 8)
        plan = self._resource_path_plan(target)
        done_key = self._resource_path_key_for(target) + "_done"
        done = set(self.state.get(done_key, []))
        empty = set(get_empty_block_names())
        skipped_blocked = 0
        self._move_to_route_frontier(plan, done)
        for step in plan:
            x, y, z, mode = self._resource_route_step_parts(step)
            key = self._build_key(x, y, z)
            if key in done:
                continue
            if mode == "bridge" and self._build_key(x, y - 1, z) in done:
                name = self._block_name_at(x, y, z)
                if name is None or name in empty:
                    done.add(key)
                    self.state[done_key] = sorted(done)
                    self.state["last_resource_path_progress_at"] = time.time()
                    self.save()
                    self._mark_build_success(x, y, z)
                    return True
            ground = self.agent.bot.blockAt(vec3.Vec3(x, y, z))
            ground_name = None if ground is None else ground.name
            if self._is_current_route_air_step(x, y, z, mode):
                done.add(key)
                self.state[done_key] = sorted(done)
                self.state["last_resource_path_progress_at"] = time.time()
                self.save()
                self._mark_build_success(x, y, z)
                return True
            if self._resource_path_walkable(x, y, z):
                moved = self._move_to_resource_step(x, y, z, mode)
                done.add(key)
                self.state[done_key] = sorted(done)
                self.state["last_resource_path_progress_at"] = time.time()
                self.save()
                self._mark_build_success(x, y, z)
                if moved:
                    index = plan.index(step)
                    reached = abs(x - int(target["x"])) + abs(z - int(target["z"])) <= 2
                    self._remember_successful_resource_route(target, plan[:index + 1], reached=reached)
                return True
            if self._is_build_position_blocked(x, y, z):
                skipped_blocked += 1
                continue
            if mode in ["fill_water", "bridge", "fill_drop"]:
                ok = self._place_resource_route_block(x, y, z, mode)
                if not ok and self._is_build_position_blocked(x, y, z):
                    self._invalidate_resource_path(target, "build step became blocked at %s" % key)
                if ok:
                    done.add(key)
                    self.state[done_key] = sorted(done)
                    self.state["last_resource_path_progress_at"] = time.time()
                    self.save()
                return ok
            if mode in ["dig_clear", "dig_jump_clear"]:
                clear_y = int(step[4]) if len(step) > 4 else y + 1
                block = self.agent.bot.blockAt(vec3.Vec3(x, clear_y, z))
                block_name = None if block is None else block.name
                if block_name is None or block_name in empty:
                    done.add(key)
                    self.state[done_key] = sorted(done)
                    self.save()
                    return True
                if not self._route_can_break_name(x, clear_y, z, block_name):
                    self._mark_build_failure(x, clear_y, z, "resource path cannot safely clear %s" % block_name, limit=1)
                    self._invalidate_resource_path(target, "unsafe dig-clear block at %s,%d,%s" % (x, clear_y, z))
                    return False
                if not self._can_safely_dig_block(block, "resource path clearing"):
                    self._invalidate_resource_path(target, "tool missing for dig-clear block at %s,%d,%s" % (x, clear_y, z))
                    return False
                try:
                    go_to_position(self.agent, x, y + 1, z, 4)
                    self.agent.bot.dig(block, timeout=45)
                    self._report("I cleared a soft block so the resource route can continue without mining stone.")
                    return True
                except Exception as e:
                    self._mark_build_failure(x, clear_y, z, "resource path dig-clear failed")
                    add_log(title=self.pack_message("Resource path dig-clear failed."), content=str(e), label="warning")
                    return False
            if ground_name == "water" and not self._is_shallow_farm_water(x, y, z):
                self._mark_build_failure(x, y, z, "resource path target is deep water", limit=1)
                continue
            if ground_name in self._liquid_block_names() and ground_name != "water":
                self._mark_build_failure(x, y, z, "resource path target is liquid", limit=1)
                continue
            if ground_name is not None and self._is_hard_route_ground_without_tool(ground_name):
                self._mark_build_failure(x, y, z, "resource path avoids hard block %s without pickaxe" % ground_name, limit=1)
                self._invalidate_resource_path(target, "hard block without pickaxe at %s" % key)
                return False
            if ground_name is not None and ground_name not in empty and ground_name not in self._walkable_ground_names():
                if not self._is_farm_clearable(ground_name):
                    self._mark_build_failure(x, y, z, "resource path blocked by %s" % ground_name, limit=1)
                    continue
                if not self._can_safely_dig_block(ground, "resource path clearing"):
                    continue
                try:
                    go_to_position(self.agent, x, y + 1, z, 4)
                    self.agent.bot.dig(ground, timeout=45)
                    self._report("I cleared a block so I can continue the dirt resource path.")
                    return True
                except Exception as e:
                    self._mark_build_failure(x, y, z, "resource path clearing failed")
                    add_log(title=self.pack_message("Resource path clearing failed."), content=str(e), label="warning")
                    return False
            if not self._can_place_supported(x, y, z, "resource path dirt placement"):
                if self._is_build_position_blocked(x, y, z):
                    self._invalidate_resource_path(target, "dirt placement target became blocked at %s" % key)
                continue
            block_name = self._best_cliff_path_block()
            if place_block(self.agent, block_name, x, y, z, "bottom", True):
                self._mark_build_success(x, y, z)
                self._remember_recent_route_block(x, y, z)
                done.add(key)
                self.state[done_key] = sorted(done)
                self.state["last_resource_path_progress_at"] = time.time()
                self.save()
                self._report("I placed dirt to extend the resource path toward %s." % target["kind"])
                return True
            self._mark_build_failure(x, y, z, "resource path dirt placement failed")
            if self._is_build_position_blocked(x, y, z):
                self._invalidate_resource_path(target, "dirt placement failed at %s" % key)
            return False
        if skipped_blocked:
            self._invalidate_resource_path(target, "skipped %d blocked steps" % skipped_blocked)
            return False
        if target.get("kind") == "tree_high" and self._advance_incomplete_tree_high_segment(target, plan):
            self._report("I completed one dirt route segment and will extend the next segment toward the high tree.")
            return True
        self._report("The dirt resource path toward %s is usable." % target["kind"])
        return True

    def _plan_resource_route_action(self):
        inv = get_item_counts(self.agent)
        target = self._resource_path_target(inv)
        if target is None:
            self._report("I do not have a resource target that needs a route right now.")
            return False
        state_key = self._resource_path_key_for(target)
        self.state.pop(state_key, None)
        self.state.pop(state_key + "_done", None)
        plan = self._resource_path_plan(target)
        if not plan:
            self._report("I could not find a safe resource route yet.", label="warning")
            return False
        jumps = len([step for step in plan if len(step) > 3 and step[3] in ["jump", "jump_hard"]])
        builds = len([step for step in plan if len(step) > 3 and step[3] in ["fill_water", "bridge", "fill_drop"]])
        clears = len([step for step in plan if len(step) > 3 and step[3] in ["dig_clear", "dig_jump_clear"]])
        hard = len([step for step in plan if len(step) > 3 and step[3] in ["walk_hard", "jump_hard"]])
        self._report("I planned a %d-step route toward %s with %d one-block jumps, %d build steps, %d soft-clearing steps, and %d hard-ground steps." % (len(plan), target["kind"], jumps, builds, clears, hard))
        return True

    def _tree_search_target(self):
        saved = self.state.get("tree_search_target")
        if isinstance(saved, dict):
            return {"kind": "tree_search", "x": int(saved["x"]), "y": int(saved["y"]), "z": int(saved["z"])}
        base = self._base()
        directions = [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]
        idx = int(self.state.get("tree_search_direction", 0) or 0) % len(directions)
        dx, dz = directions[idx]
        target = {"x": base["x"] + dx * 48, "y": base["y"], "z": base["z"] + dz * 48}
        self.state["tree_search_target"] = target
        self.save()
        return {"kind": "tree_search", "x": target["x"], "y": target["y"], "z": target["z"]}

    def _advance_tree_search_target(self):
        self.state["tree_search_direction"] = int(self.state.get("tree_search_direction", 0) or 0) + 1
        self.state.pop("tree_search_target", None)
        self.save()

    def _needs_tree_search(self, inv):
        if not self._needs_basic_wooden_tools(inv):
            return False
        if self._count_logs(inv) > 0 or self._count_planks(inv) >= 6:
            return False
        return self._best_log_block() is None

    def _find_trees(self):
        self._remember_visible_trees(force=not bool(self.state.get("known_resource_trees", [])))
        block = self._best_log_block()
        if block is not None:
            return self._collect_logs()
        remembered = self._nearest_remembered_tree()
        if remembered is not None:
            plan = self._resource_path_plan(remembered)
            if plan:
                done_key = self._resource_path_key_for(remembered) + "_done"
                done = set(self.state.get(done_key, []))
                for step in plan:
                    x, y, z, mode = self._resource_route_step_parts(step)
                    key = self._build_key(x, y, z)
                    if key in done:
                        continue
                    if not self._resource_path_walkable(x, y, z):
                        return self._build_resource_path_step()
                    ok = self._move_to_resource_step(x, y, z, mode, distance=2)
                    if ok:
                        done.add(key)
                        self.state[done_key] = sorted(done)
                        self.save()
                        index = plan.index(step)
                        reached = abs(x - int(remembered["x"])) + abs(z - int(remembered["z"])) <= 2
                        self._remember_successful_resource_route(remembered, plan[:index + 1], reached=reached)
                    return ok
        target = self._tree_search_target()
        plan = self._resource_path_plan(target)
        ok = False
        if plan:
            for step in plan:
                x, y, z, mode = self._resource_route_step_parts(step)
                if self._resource_path_walkable(x, y, z):
                    ok = self._move_to_resource_step(x, y, z, mode, distance=3)
                    if ok:
                        index = plan.index(step)
                        reached = abs(x - int(target["x"])) + abs(z - int(target["z"])) <= 2
                        self._remember_successful_resource_route(target, plan[:index + 1], reached=reached)
                    break
                return self._build_resource_path_step()
        else:
            ok = go_to_position(self.agent, target["x"], target["y"], target["z"], 8)
        block = self._best_log_block()
        if block is not None:
            self.state.pop("tree_search_target", None)
            self.save()
            self._report("I found trees near the resource path and will collect logs.")
            return True
        self._advance_tree_search_target()
        self._report("I reached a tree search point but still found no logs; I will try another direction.", label="warning")
        return ok

    def _count_logs(self, inv):
        return sum(count for name, count in inv.items() if name.endswith("_log") or name.endswith("_stem") or name == "bamboo_block")

    def _sapling_names(self):
        return [
            "oak_sapling", "spruce_sapling", "birch_sapling", "jungle_sapling",
            "acacia_sapling", "dark_oak_sapling", "mangrove_propagule", "cherry_sapling",
        ]

    def _count_saplings(self, inv):
        return sum(inv.get(name, 0) for name in self._sapling_names())

    def _best_sapling(self, inv=None):
        inv = inv or get_item_counts(self.agent)
        for name in self._sapling_names():
            if inv.get(name, 0) > 0:
                return name
        return None

    def _log_family_for_sapling(self, sapling_name):
        families = {
            "oak_sapling": ["oak_log"],
            "spruce_sapling": ["spruce_log"],
            "birch_sapling": ["birch_log"],
            "jungle_sapling": ["jungle_log"],
            "acacia_sapling": ["acacia_log"],
            "dark_oak_sapling": ["dark_oak_log"],
            "mangrove_propagule": ["mangrove_log"],
            "cherry_sapling": ["cherry_log"],
        }
        return families.get(sapling_name, [])

    def _sapling_for_log(self, log_name):
        families = {
            "oak_log": "oak_sapling",
            "spruce_log": "spruce_sapling",
            "birch_log": "birch_sapling",
            "jungle_log": "jungle_sapling",
            "acacia_log": "acacia_sapling",
            "dark_oak_log": "dark_oak_sapling",
            "mangrove_log": "mangrove_propagule",
            "cherry_log": "cherry_sapling",
        }
        return families.get(log_name)

    def _plantable_tree_ground_names(self):
        return set(["dirt", "grass_block", "podzol", "coarse_dirt", "rooted_dirt", "mud", "moss_block"])

    def _tree_replant_ground_site(self, block):
        sapling_name = self._sapling_for_log(block.name)
        if sapling_name is None:
            return None
        x, y, z = int(block.position.x), int(block.position.y), int(block.position.z)
        empty = set(get_empty_block_names())
        for gy in range(y - 1, y - 7, -1):
            ground = self._block_name_at(x, gy, z)
            above = self._block_name_at(x, gy + 1, z)
            if ground in self._plantable_tree_ground_names() and (above in empty or above in ["short_grass", "grass", "fern"]):
                return {"x": x, "y": gy, "z": z, "sapling": sapling_name, "source_log": block.name, "seen_at": int(time.time())}
        return {"x": x, "y": y - 1, "z": z, "sapling": sapling_name, "source_log": block.name, "seen_at": int(time.time())}

    def _remember_tree_replant_site(self, block):
        site = self._tree_replant_ground_site(block)
        if site is None:
            return None
        key = "%d,%d,%d" % (site["x"], site["y"], site["z"])
        sites = []
        replaced = False
        for item in self.state.get("pending_tree_replant_sites", []):
            if not isinstance(item, dict):
                continue
            item_key = "%d,%d,%d" % (int(item.get("x", 0)), int(item.get("y", 0)), int(item.get("z", 0)))
            if item_key == key:
                sites.append(site)
                replaced = True
            else:
                sites.append(item)
        if not replaced:
            sites.append(site)
        self.state["pending_tree_replant_sites"] = sites[-48:]
        self.save()
        return site

    def _pending_replant_sites(self, sapling_name):
        sites = []
        for item in self.state.get("pending_tree_replant_sites", []):
            if not isinstance(item, dict):
                continue
            if item.get("sapling") not in [sapling_name, None]:
                continue
            try:
                sites.append({"x": int(item["x"]), "y": int(item["y"]), "z": int(item["z"]), "sapling": item.get("sapling")})
            except Exception:
                continue
        return sites

    def _clear_replant_site(self, x, y, z):
        key = "%d,%d,%d" % (int(x), int(y), int(z))
        sites = []
        for item in self.state.get("pending_tree_replant_sites", []):
            if not isinstance(item, dict):
                continue
            item_key = "%d,%d,%d" % (int(item.get("x", 0)), int(item.get("y", 0)), int(item.get("z", 0)))
            if item_key != key:
                sites.append(item)
        self.state["pending_tree_replant_sites"] = sites

    def _collect_loose_saplings_nearby(self):
        for _ in range(3):
            pickup_nearby_items(self.agent, 12, 16)
            time.sleep(1.5)

    def _count_planks(self, inv):
        return sum(count for name, count in inv.items() if name.endswith("_planks"))

    def _best_log_block(self):
        names = [wood + "_log" for wood in get_wood_types()] + ["crimson_stem", "warped_stem", "bamboo_block"]
        blocks = get_nearest_blocks(self.agent, names, 40, 16)
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return blocks[0] if blocks else None
        candidates = []
        for block in blocks:
            dy = abs(int(block.position.y) - math.floor(pos.y))
            if dy > 8:
                continue
            dx = int(block.position.x) - math.floor(pos.x)
            dz = int(block.position.z) - math.floor(pos.z)
            candidates.append((dx * dx + dz * dz + dy * 4, block))
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1] if candidates else None

    def _log_block_requires_resource_path(self, block):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None or block is None:
            return False
        dx = abs(int(block.position.x) - math.floor(pos.x))
        dy = abs(int(block.position.y) - math.floor(pos.y))
        dz = abs(int(block.position.z) - math.floor(pos.z))
        if dy <= 2 and dx + dz <= 3:
            return False
        target = {"kind": "tree_high", "x": int(block.position.x), "y": int(block.position.y), "z": int(block.position.z)}
        return self._completed_high_tree_route(target) is None

    def _best_planks(self):
        inv = get_inventory_counts(self.agent)
        for wood in get_wood_types():
            if inv.get(wood + "_planks", 0) > 0 or inv.get(wood + "_log", 0) > 0:
                return wood + "_planks"
        return "oak_planks"

    def _storage_chest_position(self):
        base = self._base()
        return {"x": base["x"] - 3, "y": base["y"], "z": base["z"] + 2}

    def _storage_chest_exists(self):
        pos = self._storage_chest_position()
        block = self.agent.bot.blockAt(vec3.Vec3(pos["x"], pos["y"], pos["z"]))
        return block is not None and block.name in ["chest", "trapped_chest", "barrel"]

    def _place_storage_chest(self):
        pos = self._storage_chest_position()
        if not self._can_place_supported(pos["x"], pos["y"], pos["z"], "storage chest placement"):
            return False
        ok = place_block(self.agent, "chest", pos["x"], pos["y"], pos["z"], "bottom", True)
        if ok:
            self._mark_build_success(pos["x"], pos["y"], pos["z"])
            self.state["storage_chest"] = pos
            self.save()
            self._report("Storage chest is ready near the base.")
        return ok

    def _craft_chest(self):
        before = get_item_counts(self.agent).get("chest", 0)
        recipes = self.agent.bot.recipesFor(get_item_id("chest"), None, 1, None)
        crafting_table = None
        if recipes is None or sizeof(recipes) < 1:
            crafting_table = get_nearest_block(self.agent, "crafting_table", 32)
            if crafting_table is None:
                if get_item_counts(self.agent).get("crafting_table", 0) > 0:
                    return craft(self.agent, "crafting_table", 1)
                return False
            recipes = self.agent.bot.recipesFor(get_item_id("chest"), None, 1, crafting_table)
        if recipes is None or sizeof(recipes) < 1:
            return False
        if crafting_table is not None:
            pos = crafting_table.position
            go_to_position(self.agent, pos.x + 1, pos.y, pos.z, 1)
            try:
                self.agent.bot.unequip("hand")
            except Exception:
                pass
            try:
                self.agent.bot.lookAt(pos.offset(0.5, 0.5, 0.5))
                time.sleep(0.2)
            except Exception:
                pass
        try:
            self.agent.bot.craft(recipes[0], 1, crafting_table, timeout=60)
        except Exception as e:
            self.state["chest_craft_failures"] = int(self.state.get("chest_craft_failures", 0) or 0) + 1
            self.save()
            add_log(title=self.pack_message("Chest crafting failed."), content=str(e), label="warning")
            return False
        time.sleep(0.5)
        ok = get_item_counts(self.agent).get("chest", 0) > before
        if ok:
            self.state["chest_craft_failures"] = 0
            self.save()
        return ok

    def _inventory_stack_count(self):
        return len([item for item in self.agent.bot.inventory.items() if item is not None])

    def _should_deposit(self, inv):
        if not self._storage_chest_exists():
            return False
        if int(self.state.get("storage_deposit_failures", 0) or 0) >= 2:
            return False
        if self._inventory_stack_count() >= 28:
            return True
        stockpile_items = [
            "cobblestone", "dirt", "grass_block", "oak_log", "birch_log", "spruce_log", "jungle_log",
            "acacia_log", "dark_oak_log", "mangrove_log", "cherry_log", "oak_planks", "birch_planks",
            "spruce_planks", "jungle_planks", "acacia_planks", "dark_oak_planks", "mangrove_planks",
            "cherry_planks", "wheat", "wheat_seeds",
        ]
        return any(inv.get(name, 0) > self._keep_count(name) for name in stockpile_items)

    def _keep_count(self, item_name):
        if item_name in ["wheat_seeds"]:
            return 8
        if item_name in self._sapling_names():
            return 16
        if item_name in ["dirt", "grass_block"]:
            return 16
        if item_name.endswith("_planks"):
            return 16
        if item_name.endswith("_log") or item_name.endswith("_stem"):
            return 4
        if item_name in ["cobblestone"]:
            return 16
        if item_name in ["wheat", "bread"]:
            return 8
        return 0

    def _should_keep_item(self, item_name):
        important_fragments = [
            "pickaxe", "axe", "shovel", "hoe", "sword", "shield", "bucket", "torch", "crafting_table",
            "chest", "bread", "sapling", "propagule",
        ]
        return any(fragment in item_name for fragment in important_fragments)

    def _deposit_items(self):
        if not self._storage_chest_exists():
            return self._place_storage_chest()
        pos = self._storage_chest_position()
        go_to_position(self.agent, pos["x"], pos["y"], pos["z"], 2)
        chest_block = self.agent.bot.blockAt(vec3.Vec3(pos["x"], pos["y"], pos["z"]))
        if chest_block is None:
            return False
        deposited = 0
        chest = None
        try:
            chest = self.agent.bot.openChest(chest_block)
            time.sleep(0.5)
            counts = get_item_counts(self.agent)
            for item in list(self.agent.bot.inventory.items()):
                if item is None or self._should_keep_item(item.name):
                    continue
                keep = self._keep_count(item.name)
                available = counts.get(item.name, 0)
                amount = max(0, available - keep)
                if amount <= 0:
                    continue
                amount = min(amount, item.count)
                chest.deposit(item.type, None, amount)
                counts[item.name] = counts.get(item.name, 0) - amount
                deposited += amount
                time.sleep(0.2)
        except Exception as e:
            self.state["storage_deposit_failures"] = int(self.state.get("storage_deposit_failures", 0) or 0) + 1
            self.save()
            add_log(title=self.pack_message("Storage deposit failed."), content=str(e), label="warning")
            return False
        finally:
            if chest is not None:
                try:
                    chest.close()
                except Exception:
                    pass
        if deposited > 0:
            self.state["storage_deposit_failures"] = 0
            self.save()
            self._report("I stored %d surplus items in the base chest." % deposited)
            return True
        return False

    def _collect_logs(self):
        block = self._best_log_block()
        if block is None and self._move_to_completed_high_tree_route():
            self._remember_visible_trees(force=True)
            block = self._best_log_block()
        if block is None:
            self._report("I cannot find nearby logs, so I will stay near spawn and try again later.", label="warning")
            return False
        before = get_item_counts(self.agent).get(block.name, 0)
        replant_site = self._remember_tree_replant_site(block)
        if not self._can_safely_dig_block(block, "log collection"):
            return False
        try:
            self.agent.bot.collectBlock.collect(block, timeout=45)
            time.sleep(1.0)
            pickup_nearby_items(self.agent, 10, 12)
            self._collect_loose_saplings_nearby()
        except Exception as e:
            add_log(title=self.pack_message("Log collection attempt failed."), content=str(e), label="warning")
            return False
        after = get_item_counts(self.agent).get(block.name, 0)
        if after > before:
            self._report("I collected 1 %s for the survival stockpile." % block.name)
            inv = get_item_counts(self.agent)
            planted_any = False
            while self._count_saplings(inv) > 0:
                if not self._plant_saplings_step():
                    break
                planted_any = True
                inv = get_item_counts(self.agent)
            if not planted_any and replant_site is not None:
                self._report("I recorded the tree site and will replant it as soon as I collect a matching sapling.")
            return True
        return False

    def _should_plant_saplings(self, inv):
        if self._count_saplings(inv) < 1:
            return False
        planted = len(self.state.get("saplings_planted", []))
        nearby_saplings = get_nearest_blocks(self.agent, self._sapling_names(), 32, 8)
        nearby_logs = get_nearest_blocks(self.agent, [wood + "_log" for wood in get_wood_types()], 32, 12)
        if planted < 6:
            return True
        return len(nearby_saplings) < 4 and len(nearby_logs) < 10

    def _sapling_spacing_clear(self, x, y, z, sapling_name):
        related_logs = self._log_family_for_sapling(sapling_name)
        nearby_blockers = self._sapling_names() + related_logs
        radius = 5 if sapling_name in ["spruce_sapling", "jungle_sapling", "dark_oak_sapling"] else 4
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if dx == 0 and dz == 0:
                    continue
                for dy in range(-2, 6):
                    name = self._block_name_at(x + dx, y + dy, z + dz)
                    if name in nearby_blockers:
                        return False
        return True

    def _sapling_headroom_clear(self, x, y, z, height=7):
        empty = set(get_empty_block_names())
        allowed = empty.union(set(["short_grass", "grass", "fern", "large_fern", "tall_grass"]))
        for dy in range(1, height + 1):
            name = self._block_name_at(x, y + dy, z)
            if name is not None and name not in allowed:
                return False
        return True

    def _sapling_candidate_positions(self, sapling_name):
        base = self._base()
        positions = []
        planted = set(self.state.get("saplings_planted", []))
        empty = set(get_empty_block_names())
        for site in self._pending_replant_sites(sapling_name):
            x, y, z = site["x"], site["y"], site["z"]
            key = "%d,%d,%d" % (x, y, z)
            ground = self._block_name_at(x, y, z)
            above = self._block_name_at(x, y + 1, z)
            if key in planted or self._is_build_position_blocked(x, y + 1, z):
                continue
            if ground not in self._plantable_tree_ground_names():
                continue
            if above not in empty and above not in ["short_grass", "grass", "fern"]:
                continue
            if not self._sapling_headroom_clear(x, y, z):
                continue
            positions.append((x, y + 1, z))
        if positions:
            return positions
        for radius in range(5, 22):
            for dx in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    if max(abs(dx), abs(dz)) != radius:
                        continue
                    x = base["x"] + dx
                    z = base["z"] + dz
                    y = self._surface_y_at(x, z, base["y"], 8)
                    key = "%d,%d,%d" % (x, y, z)
                    if key in planted or self._is_build_position_blocked(x, y + 1, z):
                        continue
                    ground = self._block_name_at(x, y, z)
                    above = self._block_name_at(x, y + 1, z)
                    if ground not in self._plantable_tree_ground_names():
                        continue
                    if above not in get_empty_block_names() and above not in ["short_grass", "grass", "fern"]:
                        continue
                    if not self._sapling_spacing_clear(x, y, z, sapling_name):
                        continue
                    if not self._sapling_headroom_clear(x, y, z):
                        continue
                    positions.append((x, y + 1, z))
                    if len(positions) >= 12:
                        return positions
        return positions

    def _plant_saplings_step(self):
        inv = get_item_counts(self.agent)
        sapling_name = self._best_sapling(inv)
        if sapling_name is None:
            return False
        positions = self._sapling_candidate_positions(sapling_name)
        if not positions:
            self._report("I have saplings, but I cannot find a safely spaced planting spot yet.", label="warning")
            return False
        x, y, z = positions[0]
        if not self._can_place_supported(x, y, z, "sapling placement"):
            return False
        ok = place_block(self.agent, sapling_name, x, y, z, "bottom", True)
        verified = self._block_name_at(x, y, z)
        if ok or verified == sapling_name:
            self._mark_build_success(x, y, z)
            planted = set(self.state.get("saplings_planted", []))
            planted.add("%d,%d,%d" % (x, y - 1, z))
            self.state["saplings_planted"] = sorted(planted)
            self._clear_replant_site(x, y - 1, z)
            self.save()
            self._report("I planted %s so the forest can regrow sustainably." % sapling_name)
            return True
        self._mark_build_failure(x, y, z, "sapling placement failed")
        return False

    def _craft_planks(self):
        inv = get_inventory_counts(self.agent)
        for wood in get_wood_types():
            if inv.get(wood + "_log", 0) > 0:
                return craft(self.agent, wood + "_planks", 4)
        return craft(self.agent, "oak_planks", 4)

    def _ensure_sticks(self, count=4):
        inv = get_item_counts(self.agent)
        if inv.get("stick", 0) >= count:
            return True
        if self._count_planks(inv) < 2 and self._count_logs(inv) > 0:
            self._craft_planks()
        return craft(self.agent, "stick", 1)

    def _tool_available(self, inv, tool_kind):
        return inv.get("wooden_" + tool_kind, 0) > 0 or inv.get("stone_" + tool_kind, 0) > 0

    def _needs_basic_wooden_tools(self, inv):
        return any(not self._tool_available(inv, tool) for tool in ["axe", "shovel", "hoe"])

    def _wooden_tool_recipe(self, tool):
        recipes = {
            "wooden_pickaxe": {"planks": 3, "stick": 2},
            "wooden_axe": {"planks": 3, "stick": 2},
            "wooden_shovel": {"planks": 1, "stick": 2},
            "wooden_hoe": {"planks": 2, "stick": 2},
        }
        return recipes.get(tool, {})

    def _has_wooden_tool_items(self, inv, recipe):
        if inv.get("stick", 0) < recipe.get("stick", 0):
            return False
        return self._count_planks(inv) >= recipe.get("planks", 0)

    def _craft_wooden_tools(self):
        for tool in ["wooden_pickaxe", "wooden_axe", "wooden_shovel", "wooden_hoe"]:
            inv = get_item_counts(self.agent)
            tool_kind = tool.replace("wooden_", "")
            if self._tool_available(inv, tool_kind):
                continue
            recipe = self._wooden_tool_recipe(tool)
            if inv.get("stick", 0) < recipe.get("stick", 0):
                self._ensure_sticks(recipe.get("stick", 0))
                inv = get_item_counts(self.agent)
            if self._count_planks(inv) < recipe.get("planks", 0):
                if self._count_logs(inv) > 0:
                    return self._craft_planks()
                return self._collect_logs()
            if not self._has_wooden_tool_items(inv, recipe):
                continue
            if craft(self.agent, tool, 1):
                if tool == "wooden_pickaxe":
                    self.state["wooden_pickaxe_crafted"] = True
                    self.save()
                self._report("I crafted %s for farming and terrain work." % tool)
                return True
        return False

    def _craft_wooden_pickaxe(self):
        self._ensure_sticks(2)
        ok = craft(self.agent, "wooden_pickaxe", 1)
        if ok:
            self.state["wooden_pickaxe_crafted"] = True
            self.save()
        return ok

    def _collect_stone(self):
        inv = get_item_counts(self.agent)
        if inv.get("wooden_pickaxe", 0) < 1 and inv.get("stone_pickaxe", 0) < 1:
            return self._craft_wooden_pickaxe()
        before = inv.get("cobblestone", 0)
        blocks = get_nearest_blocks(self.agent, ["stone", "deepslate"], 32, 12)
        if not blocks:
            self._report("I cannot find exposed stone nearby, so I will keep improving the farm and shelter.", label="warning")
            return False
        block = blocks[0]
        if not self._can_safely_dig_block(block, "stone collection"):
            return False
        try:
            go_to_position(self.agent, block.position.x, block.position.y, block.position.z, 2)
            pickaxe = get_an_item_in_inventory(self.agent, "stone_pickaxe") or get_an_item_in_inventory(self.agent, "wooden_pickaxe")
            if pickaxe is not None:
                self.agent.bot.equip(pickaxe, "hand")
            self.agent.bot.dig(block, timeout=60)
            pickup_nearby_items(self.agent)
        except Exception as e:
            self.state["stone_failures"] = int(self.state.get("stone_failures", 0) or 0) + 1
            self.save()
            add_log(title=self.pack_message("Stone collection failed."), content=str(e), label="warning")
            return False
        after = get_item_counts(self.agent).get("cobblestone", 0)
        if after > before:
            self.state["stone_failures"] = 0
            self.save()
            self._report("I collected cobblestone for stone tools.")
            return True
        self.state["stone_failures"] = int(self.state.get("stone_failures", 0) or 0) + 1
        self.save()
        return False

    def _stone_tool_recipe(self, tool):
        recipes = {
            "stone_pickaxe": {"cobblestone": 3, "stick": 2},
            "stone_axe": {"cobblestone": 3, "stick": 2},
            "stone_shovel": {"cobblestone": 1, "stick": 1},
            "stone_hoe": {"cobblestone": 2, "stick": 2},
        }
        return recipes.get(tool, {})

    def _has_recipe_items(self, inv, recipe):
        return all(inv.get(name, 0) >= count for name, count in recipe.items())

    def _can_craft_any_stone_tool(self, inv, tools):
        if inv.get("stick", 0) < 2 and self._count_planks(inv) >= 2:
            return True
        return any(self._has_recipe_items(inv, self._stone_tool_recipe(tool)) for tool in tools)

    def _craft_stone_tools(self):
        for tool in ["stone_pickaxe", "stone_axe", "stone_shovel", "stone_hoe"]:
            inv = get_item_counts(self.agent)
            if inv.get(tool, 0) >= 1:
                continue
            recipe = self._stone_tool_recipe(tool)
            if inv.get("stick", 0) < recipe.get("stick", 0):
                self._ensure_sticks(recipe.get("stick", 0))
                inv = get_item_counts(self.agent)
            if not self._has_recipe_items(inv, recipe):
                continue
            if craft(self.agent, tool, 1):
                return True
        return False

    def _collect_seeds(self):
        before = get_item_counts(self.agent).get("wheat_seeds", 0)
        collect_blocks(self.agent, "short_grass", 8)
        after = get_item_counts(self.agent).get("wheat_seeds", 0)
        if after > before:
            self._report("I collected wheat seeds for the farm.")
            return True
        self._report("I tried collecting seeds but did not get many yet.", label="warning")
        return False

    def _base(self):
        configured = self.agent.configs.get("survival_loop_base", None)
        if isinstance(configured, dict):
            desired = {"x": int(configured["x"]), "y": int(configured["y"]), "z": int(configured["z"])}
            if self.state.get("base") != desired:
                self.state["base"] = desired
                self.state["shelter_blocks_done"] = []
                self.state["farm_origin"] = None
                self.state["farm_blocks_done"] = []
                self.save()
            return self.state["base"]
        base = self.state.get("base")
        if base is None or int(base.get("y", 0)) > 90:
            pos = get_entity_position(self.agent.bot.entity)
            y = 65 if pos is None or math.floor(pos.y) > 90 else math.floor(pos.y)
            x = 3 if pos is None or math.floor(pos.y) > 90 else math.floor(pos.x)
            z = 8 if pos is None or math.floor(pos.y) > 90 else math.floor(pos.z)
            self.state["base"] = {"x": x, "y": y, "z": z}
            self.state["shelter_blocks_done"] = []
            self.state["farm_origin"] = None
            self.state["farm_blocks_done"] = []
            self.save()
        return self.state["base"]

    def _shelter_plan(self):
        base = self._base()
        bx, by, bz = base["x"], base["y"], base["z"]
        plan = []
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                if abs(dx) == 2 or abs(dz) == 2:
                    if dx == 0 and dz == -2:
                        continue
                    plan.append((bx + dx, by, bz + dz))
                    plan.append((bx + dx, by + 1, bz + dz))
                plan.append((bx + dx, by + 2, bz + dz))
        return plan

    def _build_shelter_step(self):
        block_name = self._best_planks()
        done = set(self.state.get("shelter_blocks_done", []))
        for x, y, z in self._shelter_plan():
            key = "%d,%d,%d" % (x, y, z)
            if key in done or self._is_build_position_blocked(x, y, z):
                continue
            if not self._can_place_supported(x, y, z, "shelter block placement"):
                continue
            ok = place_block(self.agent, block_name, x, y, z, "bottom", True)
            if ok:
                self._mark_build_success(x, y, z)
                done.add(key)
                self.state["shelter_blocks_done"] = sorted(done)
                self.save()
                return True
            self._mark_build_failure(x, y, z, "shelter block placement failed")
            return False
        self._report("The compact shelter shell is complete enough for now.")
        return True

    def _farm_origin(self):
        if self.state.get("farm_origin") is None:
            base = self._base()
            self.state["farm_origin"] = {"x": base["x"] + 6, "y": base["y"] - 1, "z": base["z"]}
            self.state["farm_blocks_done"] = []
            self.save()
        return self.state["farm_origin"]

    def _fixed_farm_positions(self):
        origin = self._farm_origin()
        ox, oy, oz = origin["x"], origin["y"], origin["z"]
        return [(ox + dx, oy, oz + dz) for dx in range(-2, 3) for dz in range(-2, 3)]

    def _block_name_at(self, x, y, z):
        block = self.agent.bot.blockAt(vec3.Vec3(x, y, z))
        return None if block is None else block.name

    # --- 3D Terrain Sensing ---

    def _terrain_scan_radius(self):
        return int(self.agent.configs.get("terrain_scan_radius", 6))

    def _terrain_scan_interval_seconds(self):
        return float(self.agent.configs.get("terrain_scan_interval_seconds", 60))

    def _maybe_update_terrain_snapshot(self):
        now = time.time()
        last = float(self.state.get("terrain_snapshot_at", 0) or 0)
        if now - last < self._terrain_scan_interval_seconds():
            return False
        try:
            self.state["terrain_snapshot"] = self._terrain_snapshot()
            self.state["terrain_snapshot_at"] = now
            self.save()
            return True
        except Exception as e:
            add_log(title=self.pack_message("Terrain sensing failed."), content=str(e), label="warning")
            self.state["terrain_snapshot_at"] = now
            self.save()
            return False

    def _terrain_column(self, x, z, center_y, vertical_radius=8):
        empty = set(get_empty_block_names())
        liquids = self._liquid_block_names()
        dangers = self._farm_danger_blocks()
        column = {
            "x": int(x),
            "z": int(z),
            "surface_y": None,
            "surface": None,
            "headroom": 0,
            "water_y": None,
            "water_depth": 0,
            "danger": False,
            "tree": False,
            "walkable": False,
        }
        for y in range(int(center_y) + vertical_radius, int(center_y) - vertical_radius - 1, -1):
            name = self._block_name_at(x, y, z)
            if name in dangers or name in ["lava", "flowing_lava"]:
                column["danger"] = True
            if name is not None and (name.endswith("_log") or name.endswith("_stem")):
                column["tree"] = True
            if name in ["water", "flowing_water"] and column["water_y"] is None:
                column["water_y"] = y
                depth = 1
                yy = y - 1
                while self._block_name_at(x, yy, z) in ["water", "flowing_water"] and depth < 8:
                    depth += 1
                    yy -= 1
                column["water_depth"] = depth
            if name is None or name in empty or name in liquids:
                continue
            head = self._block_name_at(x, y + 1, z)
            if head is None or head in empty:
                column["surface_y"] = y
                column["surface"] = name
                headroom = 0
                for dy in range(1, 7):
                    above = self._block_name_at(x, y + dy, z)
                    if above is None or above in empty:
                        headroom += 1
                    else:
                        break
                column["headroom"] = headroom
                column["walkable"] = name not in liquids and name not in dangers and headroom >= 2
                break
        return column

    def _terrain_snapshot(self, radius=None):
        pos = get_entity_position(self.agent.bot.entity)
        if pos is None:
            return {}
        radius = radius or self._terrain_scan_radius()
        cx, cy, cz = math.floor(pos.x), math.floor(pos.y), math.floor(pos.z)
        columns = {}
        walkable = []
        shallow_water = []
        deep_water = []
        cliffs = []
        farmable = []
        trees = []
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                x, z = cx + dx, cz + dz
                col = self._terrain_column(x, z, cy)
                columns[(x, z)] = col
                if col["walkable"]:
                    walkable.append([x, col["surface_y"], z])
                if col["water_depth"] == 1:
                    shallow_water.append([x, col["water_y"], z])
                elif col["water_depth"] > 1:
                    deep_water.append([x, col["water_y"], z])
                if col["tree"]:
                    trees.append([x, col["surface_y"] or cy, z])
        for (x, z), col in columns.items():
            sy = col["surface_y"]
            if sy is None:
                continue
            max_drop = 0
            max_rise = 0
            for ox, oz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                other = columns.get((x + ox, z + oz))
                if other is None or other["surface_y"] is None:
                    continue
                delta = other["surface_y"] - sy
                max_rise = max(max_rise, delta)
                max_drop = max(max_drop, -delta)
            if max_drop >= 3 or max_rise >= 3:
                cliffs.append([x, sy, z, max_drop, max_rise])
            if col["walkable"] and col["surface"] in ["dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mud"]:
                if self._is_water_near(x, sy, z, 4) and col["headroom"] >= 2:
                    farmable.append([x, sy, z])
        summary = {
            "center": {"x": cx, "y": cy, "z": cz},
            "radius": radius,
            "walkable_count": len(walkable),
            "shallow_water_count": len(shallow_water),
            "deep_water_count": len(deep_water),
            "cliff_count": len(cliffs),
            "farmable_count": len(farmable),
            "tree_count": len(trees),
            "nearest_walkable": walkable[:12],
            "nearest_shallow_water": shallow_water[:12],
            "nearest_deep_water": deep_water[:8],
            "cliff_edges": cliffs[:12],
            "farmable_tiles": farmable[:18],
            "tree_columns": trees[:12],
        }
        return summary

    def _sense_terrain(self):
        snapshot = self._terrain_snapshot()
        if not snapshot:
            return False
        self.state["terrain_snapshot"] = snapshot
        self.save()
        self._report(
            "Terrain scan: %d walkable, %d shallow water, %d cliffs, %d farmable, %d tree columns."
            % (
                snapshot.get("walkable_count", 0),
                snapshot.get("shallow_water_count", 0),
                snapshot.get("cliff_count", 0),
                snapshot.get("farmable_count", 0),
                snapshot.get("tree_count", 0),
            )
        )
        return True

    def _chunk_coords(self, x, z):
        return (math.floor(x / 16), math.floor(z / 16))

    def _is_water_near(self, x, y, z, radius=4, exclude=None):
        exclude = exclude or set()
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dz)) > radius:
                    continue
                key = "%d,%d,%d" % (x + dx, y, z + dz)
                above_key = "%d,%d,%d" % (x + dx, y + 1, z + dz)
                if key in exclude or above_key in exclude:
                    continue
                if self._block_name_at(x + dx, y, z + dz) in ["water", "flowing_water"] or self._block_name_at(x + dx, y + 1, z + dz) in ["water", "flowing_water"]:
                    return True
        return False

    def _is_hydrated_farm_tile(self, x, y, z):
        return self._is_water_near(x, y, z, 4)

    def _farm_daylight_scan_height(self):
        return int(self.agent.configs.get("farm_daylight_scan_height", 18))

    def _farm_max_water_level_delta(self):
        return int(self.agent.configs.get("farm_max_water_level_delta", 3))

    def _farm_min_sky_light(self):
        return int(self.agent.configs.get("farm_min_sky_light", 12))

    def _farm_initial_target_tiles(self):
        return int(self.agent.configs.get("farm_initial_target_tiles", 24))

    def _farm_expansion_target_tiles(self):
        return int(self.agent.configs.get("farm_expansion_target_tiles", 48))

    def _farm_sky_light(self, x, y, z):
        try:
            for dy in [1, 2, 0]:
                block = self.agent.bot.blockAt(vec3.Vec3(x, y + dy, z))
                if block is None:
                    continue
                value = getattr(block, "skyLight", None)
                if value is None:
                    continue
                return int(value)
        except Exception:
            return None
        return None

    def _farm_daylight_clear_cost(self, x, y, z):
        empty = set(get_empty_block_names())
        cost = 0
        for dy in range(1, self._farm_daylight_scan_height() + 1):
            name = self._block_name_at(x, y + dy, z)
            if name is None or name in empty:
                continue
            if dy <= self._farm_terrace_cut_limit() and self._is_farm_clearable(name):
                cost += 1
                continue
            return None
        return cost

    def _is_daylight_farm_tile(self, x, y, z):
        sky_light = self._farm_sky_light(x, y, z)
        if sky_light is not None and sky_light < self._farm_min_sky_light():
            return False
        return self._farm_daylight_clear_cost(x, y, z) is not None

    def _is_abandoned_farm_position(self, x, y, z):
        abandoned = self.state.get("abandoned_farm_positions", {}) or {}
        return "%d,%d,%d" % (x, y, z) in abandoned

    def _filter_hydrated_farm_positions(self, positions):
        filtered = []
        seen = set()
        for pos in positions or []:
            if not isinstance(pos, (list, tuple)) or len(pos) < 3:
                continue
            x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
            key = "%d,%d,%d" % (x, y, z)
            if key in seen:
                continue
            if self._is_abandoned_farm_position(x, y, z):
                continue
            if not self._is_hydrated_farm_tile(x, y, z):
                continue
            if not self._is_daylight_farm_tile(x, y, z):
                continue
            if self._farm_leveling_cost(x, y, z) is None:
                continue
            seen.add(key)
            filtered.append((x, y, z))
        return filtered

    def _farm_clearable_blocks(self):
        return set([
            "short_grass", "tall_grass", "grass", "fern", "large_fern", "dead_bush", "snow",
            "pink_petals", "wildflowers", "leaf_litter",
            "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mud",
            "sand", "red_sand", "gravel", "clay",
        ])

    def _is_farm_clearable(self, block_name):
        return block_name in self._farm_clearable_blocks() or (block_name is not None and "leaves" in block_name)

    def _farm_danger_blocks(self):
        return set(["lava", "fire", "campfire", "soul_campfire", "cactus", "sweet_berry_bush", "magma_block"])

    def _farm_terrace_cut_limit(self):
        return 5

    def _solid_farm_base(self, block_name):
        empty = set(get_empty_block_names())
        bad = empty.union(self._liquid_block_names()).union(self._farm_danger_blocks())
        return block_name is not None and block_name not in bad

    def _is_shallow_farm_water(self, x, y, z):
        if self._block_name_at(x, y, z) != "water":
            return False
        if self._block_name_at(x, y + 1, z) == "water":
            return False
        if not self._solid_farm_base(self._block_name_at(x, y - 1, z)):
            return False
        return self._is_water_near(x, y, z, 4, exclude=set(["%d,%d,%d" % (x, y, z)]))

    def _can_place_farm_dirt(self, x, y, z):
        if self._is_shallow_farm_water(x, y, z):
            return self._has_build_support(x, y, z)
        return self._can_place_supported(x, y, z, "farm dirt placement")

    def _farm_leveling_cost(self, x, y, z):
        ground = self._block_name_at(x, y, z)
        if ground == "water" and self._is_shallow_farm_water(x, y, z):
            cost = 2
        elif ground in [None, "water", "lava"] or ground in self._farm_danger_blocks():
            return None
        else:
            cost = 0
        for clear_y in range(y + 1, y + self._farm_terrace_cut_limit() + 1):
            name = self._block_name_at(x, clear_y, z)
            if name is None or name in get_empty_block_names():
                continue
            if not self._is_farm_clearable(name):
                return None
            cost += 1
        below = self._block_name_at(x, y - 1, z)
        if not self._solid_farm_base(below):
            cost += 4
        return cost

    def _farm_hazard_penalty(self, x, y, z):
        penalty = 0
        for dx in range(-1, 2):
            for dz in range(-1, 2):
                for dy in range(-1, 2):
                    name = self._block_name_at(x + dx, y + dy, z + dz)
                    if name in self._farm_danger_blocks():
                        penalty += 8
                    elif name in ["water", "lava"] and dy == 0 and (dx != 0 or dz != 0):
                        penalty += 1
        return penalty

    def _farm_flatness_cost(self, x, y, z):
        cost = 0
        for dx, dz in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            if self._block_name_at(x + dx, y, z + dz) in ["dirt", "grass_block", "farmland"]:
                continue
            if self._block_name_at(x + dx, y + 1, z + dz) in ["dirt", "grass_block", "farmland"]:
                cost += 1
            else:
                cost += 2
        return cost

    def _riverbank_farm_score(self, x, y, z, base, target_y):
        if abs(y - target_y) > self._farm_max_water_level_delta():
            return None
        level_cost = self._farm_leveling_cost(x, y, z)
        if level_cost is None:
            return None
        hydrated = self._is_water_near(x, y, z, 4)
        if not hydrated:
            return None
        sky_light = self._farm_sky_light(x, y, z)
        if sky_light is not None and sky_light < self._farm_min_sky_light():
            return None
        daylight_cost = self._farm_daylight_clear_cost(x, y, z)
        if daylight_cost is None:
            return None
        distance = abs(x - base["x"]) + abs(z - base["z"])
        vertical = abs(y - target_y)
        hazard = self._farm_hazard_penalty(x, y, z)
        flatness = self._farm_flatness_cost(x, y, z)
        chunk_x, chunk_z = self._chunk_coords(x, z)
        base_chunk_x, base_chunk_z = self._chunk_coords(base["x"], base["z"])
        chunk_cost = abs(chunk_x - base_chunk_x) + abs(chunk_z - base_chunk_z)
        return 120 - 8 * level_cost - 6 * daylight_cost - 10 * vertical - 3 * flatness - 2 * distance - 12 * hazard - chunk_cost

    def _connected_farm_component(self, start, remaining):
        stack = [start]
        component = set()
        while stack:
            key = stack.pop()
            if key in component or key not in remaining:
                continue
            component.add(key)
            x, y, z = key
            for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                neighbor = (x + dx, y, z + dz)
                if neighbor in remaining and neighbor not in component:
                    stack.append(neighbor)
        return component

    def _order_farm_patch(self, component, score_by_key, target_size):
        seed = max(component, key=lambda key: score_by_key.get(key, -9999))
        selected = [seed]
        selected_set = set([seed])
        frontier = set()
        while len(selected) < target_size:
            x, y, z = selected[-1]
            for sx, sy, sz in list(selected_set):
                for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                    neighbor = (sx + dx, sy, sz + dz)
                    if neighbor in component and neighbor not in selected_set:
                        frontier.add(neighbor)
            if not frontier:
                break
            best = max(frontier, key=lambda key: score_by_key.get(key, -9999))
            frontier.remove(best)
            selected.append(best)
            selected_set.add(best)
        return selected

    def _select_farm_expansion_patch(self, scored_candidates):
        if not scored_candidates:
            return []
        target_size = self._farm_expansion_target_tiles()
        score_by_key = {}
        remaining = set()
        for score, x, y, z in scored_candidates:
            key = (int(x), int(y), int(z))
            score_by_key[key] = max(score_by_key.get(key, -9999), score)
            remaining.add(key)
        components = []
        unvisited = set(remaining)
        while unvisited:
            start = next(iter(unvisited))
            component = self._connected_farm_component(start, remaining)
            unvisited.difference_update(component)
            if len(component) < 3:
                continue
            ordered = self._order_farm_patch(component, score_by_key, target_size)
            top_scores = sorted([score_by_key[key] for key in ordered], reverse=True)
            patch_score = sum(top_scores) + 6 * len(ordered)
            xs = [key[0] for key in ordered]
            zs = [key[2] for key in ordered]
            width = max(xs) - min(xs) + 1
            depth = max(zs) - min(zs) + 1
            compactness = len(ordered) / float(max(1, width * depth))
            patch_score += 20 * compactness
            components.append((patch_score, len(ordered), ordered))
        if not components:
            return []
        components.sort(key=lambda item: (-item[0], -item[1]))
        selected = components[0][2]
        self.state["farm_expansion_plan"] = {
            "target_tiles": target_size,
            "planned_tiles": len(selected),
            "y": int(selected[0][1]),
            "bounds": {
                "min_x": min(key[0] for key in selected),
                "max_x": max(key[0] for key in selected),
                "min_z": min(key[2] for key in selected),
                "max_z": max(key[2] for key in selected),
            },
        }
        self.save()
        return [(x, y, z) for x, y, z in selected]

    def _riverbank_candidate_positions(self):
        water_blocks = get_nearest_blocks(self.agent, ["water"], 72, 96)
        base = self._base()
        if not water_blocks:
            return []
        preferred_y = self._riverbank_search_target()["y"] - 1
        candidates = []
        seen = set()
        for water in water_blocks:
            wx, wy, wz = int(water.position.x), int(water.position.y), int(water.position.z)
            for dx in range(-4, 5):
                for dz in range(-4, 5):
                    if max(abs(dx), abs(dz)) > 4 or (dx == 0 and dz == 0):
                        continue
                    x, y, z = wx + dx, wy, wz + dz
                    key = "%d,%d,%d" % (x, y, z)
                    if key in seen:
                        continue
                    seen.add(key)
                    if self._is_abandoned_farm_position(x, y, z):
                        continue
                    score = self._riverbank_farm_score(x, y, z, base, preferred_y)
                    if score is None:
                        continue
                    candidates.append((score, x, y, z))
        return self._select_farm_expansion_patch(candidates)

    def _has_riverbank_farm_plan(self):
        saved = self.state.get("riverbank_farm_positions")
        if isinstance(saved, list) and len(saved) >= 3:
            filtered = self._filter_hydrated_farm_positions(saved)
            if len(filtered) >= 3:
                if len(filtered) != len(saved):
                    done = set(self.state.get("farm_blocks_done", []))
                    allowed = set("%d,%d,%d" % pos for pos in filtered)
                    self.state["riverbank_farm_positions"] = [list(pos) for pos in filtered]
                    self.state["farm_blocks_done"] = sorted(done.intersection(allowed))
                    self.save()
                return True
            self.state.pop("riverbank_farm_positions", None)
            self.state["farm_blocks_done"] = []
            self.save()
        return len(self._riverbank_candidate_positions()) >= 3

    def _riverbank_search_target(self):
        target = self.state.get("riverbank_search_target")
        if isinstance(target, dict):
            return {"x": int(target["x"]), "y": int(target["y"]), "z": int(target["z"])}
        return {"x": 32, "y": 63, "z": 32}

    def _find_riverbank(self):
        target = self._riverbank_search_target()
        ok = go_to_position(self.agent, target["x"], target["y"], target["z"], 6)
        positions = self._riverbank_candidate_positions()
        if len(positions) >= 3:
            self.state["riverbank_farm_positions"] = [list(pos) for pos in positions]
            self.state["farm_blocks_done"] = []
            self.save()
            self._report("I found river water and will open the farm along the shoreline.")
            return True
        return ok

    def _riverbank_farm_positions(self):
        saved = self.state.get("riverbank_farm_positions")
        if isinstance(saved, list) and len(saved) >= 3:
            filtered = self._filter_hydrated_farm_positions(saved)
            if len(filtered) >= 3:
                if len(filtered) != len(saved):
                    done = set(self.state.get("farm_blocks_done", []))
                    allowed = set("%d,%d,%d" % pos for pos in filtered)
                    self.state["riverbank_farm_positions"] = [list(pos) for pos in filtered]
                    self.state["farm_blocks_done"] = sorted(done.intersection(allowed))
                    self.save()
                return filtered
            self.state.pop("riverbank_farm_positions", None)
            self.state["farm_blocks_done"] = []
            self.save()
        positions = self._riverbank_candidate_positions()
        if len(positions) >= 3:
            self.state["farm_mode"] = "riverbank"
            self.state["riverbank_farm_positions"] = [list(pos) for pos in positions]
            self.state["farm_blocks_done"] = []
            self.save()
            self._report("I found a riverbank and will develop hydrated farmland along the shore.")
            return positions
        return []

    def _farm_positions(self):
        riverbank = self._riverbank_farm_positions()
        if riverbank:
            return riverbank
        return self._fixed_farm_positions()

    def _abandon_farm_position(self, x, y, z, reason):
        key = "%d,%d,%d" % (x, y, z)
        changed = False
        saved = self.state.get("riverbank_farm_positions")
        if isinstance(saved, list):
            kept = []
            for pos in saved:
                if not isinstance(pos, (list, tuple)) or len(pos) < 3:
                    continue
                pos_key = "%d,%d,%d" % (int(pos[0]), int(pos[1]), int(pos[2]))
                if pos_key == key:
                    changed = True
                    continue
                kept.append([int(pos[0]), int(pos[1]), int(pos[2])])
            self.state["riverbank_farm_positions"] = kept
        done = set(self.state.get("farm_blocks_done", []))
        if key in done:
            done.remove(key)
            self.state["farm_blocks_done"] = sorted(done)
            changed = True
        abandoned = dict(self.state.get("abandoned_farm_positions", {}) or {})
        abandoned[key] = {"reason": reason, "t": time.time()}
        self.state["abandoned_farm_positions"] = abandoned
        self.save()
        add_log(
            title=self.pack_message("Abandoned farm position."),
            content="%s at %s; trying another hydrated riverbank tile." % (reason, key),
            label="warning",
        )

    def _farm_plot_ready(self):
        done = set(self.state.get("farm_blocks_done", []))
        return len(done) >= len(self._farm_positions())

    def _prepared_farm_tile_count(self):
        done = set(self.state.get("farm_blocks_done", []))
        if not done:
            return 0
        positions = set("%d,%d,%d" % pos for pos in self._farm_positions())
        return len(done.intersection(positions))

    def _prepare_farm_plot(self):
        inv = get_item_counts(self.agent)
        done = set(self.state.get("farm_blocks_done", []))
        empty = set(get_empty_block_names())
        for x, y, z in self._farm_positions():
            key = "%d,%d,%d" % (x, y, z)
            if not self._is_hydrated_farm_tile(x, y, z):
                continue
            if self._is_build_position_blocked(x, y, z):
                continue
            pos = vec3.Vec3(x, y, z)
            block = self.agent.bot.blockAt(pos)
            abandoned_tile = False
            for clear_y in range(y + self._farm_terrace_cut_limit(), y, -1):
                above = self.agent.bot.blockAt(vec3.Vec3(x, clear_y, z))
                if above is None or above.name in empty:
                    continue
                if not self._is_farm_clearable(above.name):
                    self._mark_build_failure(x, clear_y, z, "farm terrace blocked by %s" % above.name)
                    add_log(title=self.pack_message("Farm terrace blocked."), content="%s at (%d, %d, %d)" % (above.name, x, clear_y, z), label="warning")
                    self._abandon_farm_position(x, y, z, "farm terrace blocked by %s" % above.name)
                    abandoned_tile = True
                    break
                try:
                    go_to_position(self.agent, x, y + 1, z, 4)
                except Exception as e:
                    self._mark_build_failure(x, clear_y, z, "farm terrace approach failed")
                    add_log(title=self.pack_message("Farm terrace approach failed."), content=str(e), label="warning")
                    self._abandon_farm_position(x, y, z, "farm terrace approach failed")
                    abandoned_tile = True
                    break
                above = self.agent.bot.blockAt(vec3.Vec3(x, clear_y, z))
                if above is None or above.name in empty:
                    return True
                if not self._can_safely_dig_block(above, "farm terrace clearing"):
                    self._abandon_farm_position(x, y, z, "farm terrace cannot clear %s" % above.name)
                    abandoned_tile = True
                    break
                try:
                    self.agent.bot.dig(above, timeout=45)
                    self._report("I dug overhead blocks to open a terrace for farmland.")
                    return True
                except Exception as e:
                    self._mark_build_failure(x, clear_y, z, "farm clearing failed")
                    add_log(title=self.pack_message("Farm clearing failed."), content=str(e), label="warning")
                    self._abandon_farm_position(x, y, z, "farm terrace clearing failed")
                    abandoned_tile = True
                    break
            if abandoned_tile:
                continue
            if block is None or block.name not in ["dirt", "grass_block", "farmland"]:
                if block is not None and self._is_farm_clearable(block.name):
                    try:
                        go_to_position(self.agent, x, y + 1, z, 4)
                    except Exception as e:
                        self._mark_build_failure(x, y, z, "farm ground approach failed")
                        add_log(title=self.pack_message("Farm ground approach failed."), content=str(e), label="warning")
                        self._abandon_farm_position(x, y, z, "farm ground approach failed")
                        continue
                    block = self.agent.bot.blockAt(pos)
                    if block is None or block.name in ["dirt", "grass_block", "farmland"]:
                        continue
                    if not self._can_safely_dig_block(block, "farm ground removal"):
                        self._abandon_farm_position(x, y, z, "farm ground cannot remove %s" % block.name)
                        continue
                    try:
                        self.agent.bot.dig(block, timeout=45)
                        self._report("I removed rough riverbank ground so I can replace it with farm soil.")
                        return True
                    except Exception as e:
                        self._mark_build_failure(x, y, z, "farm ground removal failed")
                        add_log(title=self.pack_message("Farm ground removal failed."), content=str(e), label="warning")
                        self._abandon_farm_position(x, y, z, "farm ground removal failed")
                        continue
                if inv.get("dirt", 0) + inv.get("grass_block", 0) < 1:
                    return collect_blocks(self.agent, "dirt", 8)
                placed = False
                if not self._can_place_farm_dirt(x, y, z):
                    continue
                try:
                    placed = place_block(self.agent, "dirt", x, y, z, "top", True)
                except Exception as e:
                    add_log(title=self.pack_message("Farm dirt placement needs verification."), content=str(e), label="warning")
                    placed = "blockUpdate" in str(e)
                verified = self.agent.bot.blockAt(vec3.Vec3(x, y, z))
                if placed or (verified is not None and verified.name in ["dirt", "grass_block", "farmland"]):
                    self._mark_build_success(x, y, z)
                    done.add(key)
                    self.state["farm_blocks_done"] = sorted(done)
                    self.save()
                    return True
                self._mark_build_failure(x, y, z, "farm dirt placement failed")
                self._abandon_farm_position(x, y, z, "farm dirt placement failed")
                continue
            if key not in done:
                self._mark_build_success(x, y, z)
                done.add(key)
                self.state["farm_blocks_done"] = sorted(done)
                self.save()
                return True
            continue
        self._report("The farm plot is flat enough for planting.")
        return True

    def _farm_step(self):
        inv = get_item_counts(self.agent)
        mature = self._nearest_mature_wheat()
        if mature is not None:
            collect_blocks(self.agent, "wheat", 1)
            return True
        if inv.get("wheat_seeds", 0) < 1:
            return self._collect_seeds()
        if inv.get("wooden_hoe", 0) < 1 and inv.get("stone_hoe", 0) < 1:
            if inv.get("stick", 0) < 2:
                craft(self.agent, "stick", 1)
            if craft(self.agent, "wooden_hoe", 1):
                return True
        return self._till_and_plant()

    def _has_mature_wheat_nearby(self):
        return self._nearest_mature_wheat() is not None

    def _nearest_mature_wheat(self):
        blocks = get_nearest_blocks(self.agent, ["wheat"], 24, 8)
        for block in blocks:
            try:
                age = block.getProperties().get("age")
                if str(age) == "7":
                    return block
            except Exception:
                continue
        return None

    def _till_and_plant(self):
        water = get_nearest_block(self.agent, "water", 24)
        base = self._base()
        centers = []
        if water is not None:
            centers.append(water.position)
        entity_pos = get_entity_position(self.agent.bot.entity)
        if entity_pos is not None:
            centers.append(entity_pos)
        candidates = []
        for x, y, z in self._farm_positions():
            if not self._is_hydrated_farm_tile(x, y, z):
                continue
            pos = vec3.Vec3(x, y, z)
            block = self.agent.bot.blockAt(pos)
            above = self.agent.bot.blockAt(vec3.Vec3(x, y + 1, z))
            if block is not None and above is not None and block.name in ["farmland", "dirt", "grass_block"] and above.name in get_empty_block_names():
                candidates.append(block)
        for center in centers:
            for dy in range(-2, 2):
                for dx in range(-5, 6):
                    for dz in range(-5, 6):
                        pos = vec3.Vec3(center.x + dx, center.y + dy, center.z + dz)
                        block = self.agent.bot.blockAt(pos)
                        above = self.agent.bot.blockAt(vec3.Vec3(pos.x, pos.y + 1, pos.z))
                        if not self._is_hydrated_farm_tile(int(pos.x), int(pos.y), int(pos.z)):
                            continue
                        if block is not None and above is not None and block.name in ["farmland", "dirt", "grass_block"] and above.name in get_empty_block_names():
                            candidates.append(block)
            if candidates:
                break
        if not candidates:
            self.state["farm_failures"] = int(self.state.get("farm_failures", 0) or 0) + 1
            self.save()
            self._report("I cannot find a clear dirt patch for farming yet; I will switch tasks if this keeps failing.", label="warning")
            return False
        self.state["farm_failures"] = 0
        self.save()
        target = candidates[0]
        go_to_position(self.agent, target.position.x, target.position.y, target.position.z, 3)
        farmland = target
        if target.name != "farmland":
            hoe = "wooden_hoe" if get_item_counts(self.agent).get("wooden_hoe", 0) > 0 else "stone_hoe"
            hoe_item = None
            for item in self.agent.bot.inventory.items():
                if item is not None and item.name == hoe:
                    hoe_item = item
                    break
            if hoe_item is not None:
                self.agent.bot.equip(hoe_item, "hand")
                time.sleep(0.2)
                self.agent.bot.activateBlock(target)
                time.sleep(0.5)
            farmland = self.agent.bot.blockAt(target.position)
        seed_item = None
        for item in self.agent.bot.inventory.items():
            if item is not None and item.name == "wheat_seeds":
                seed_item = item
                break
        if seed_item is not None and farmland is not None:
            self.agent.bot.equip(seed_item, "hand")
            time.sleep(0.2)
            self.agent.bot.activateBlock(farmland)
            self._report("I tilled and planted a wheat seed for the starter farm.")
            return True
        return False
